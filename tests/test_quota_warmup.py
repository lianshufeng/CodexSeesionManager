import io
import json
import tempfile
import time
import tkinter as tk
import unittest
from dataclasses import replace
from pathlib import Path
from threading import Event
from unittest.mock import Mock, patch

from app.models import LoadStrategy, QuotaWindow
from app.services.app_config_service import AppConfig, AppConfigService
from app.services.auth_sync_service import AuthFileRow
from app.services.auth_usage_service import AuthQuotaItem
from app.services.quota_warmup_service import DEFAULT_WARMUP_MODEL, QuotaWarmupService
from app.utils.chatgpt_usage_fetcher import UsageFetchResult
from app.utils.codex_warmup_client import CodexWarmupClient, WarmupRejected


def usage(reset=19000, used=0, duration=18000, secondary_used=20):
    return UsageFetchResult(quota="100%/80%", raw={"rate_limit": {
        "primary_window": {"used_percent": used, "reset_at": reset, "limit_window_seconds": duration},
        "secondary_window": {"used_percent": secondary_used, "reset_at": 200000, "limit_window_seconds": 604800},
    }})


class QuotaWarmupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "warmup.json"
        self.auth, self.usage, self.client, self.fetcher = Mock(), Mock(), Mock(), Mock()
        self.row = AuthFileRow("account", "refresh", "", file_name="account.json", access_token="token")
        self.auth.list_auth_rows.return_value = [self.row]
        self.usage.item_for.return_value = AuthQuotaItem("refresh", "account", "100%/80%", windows=usage().windows, observed_at=1000)
        self.fetcher.fetch.side_effect = [usage(), usage(reset=19000, used=1)]
        self.service = self.make_service()
        self.service.configure(True, DEFAULT_WARMUP_MODEL)
        self.service._observe("account", DEFAULT_WARMUP_MODEL, usage(reset=18940).windows, 940)

    def make_service(self):
        service = QuotaWarmupService(self.auth, self.usage, client=self.client, fetcher=self.fetcher, state_path=self.path)
        service.set_proxy_provider(lambda: "127.0.0.1:1088")
        return service

    def check(self, service=None):
        with patch("app.services.quota_warmup_service.time.time", return_value=1000):
            (service or self.service).check_once()

    def check_at(self, now, reset=None, *, used=0, service=None):
        self.usage.item_for.return_value = replace(
            self.usage.item_for.return_value,
            windows=usage(reset=reset if reset is not None else now + 18000, used=used).windows,
            observed_at=now)
        with patch("app.services.quota_warmup_service.time.time", return_value=now):
            (service or self.service).check_once()

    def test_ping_uses_account_and_model_then_confirms_window(self):
        self.check()
        self.client.ping.assert_called_once_with("token", "account", "127.0.0.1:1088", "gpt-6-luna", self.service._stop)
        self.auth.increment_traffic_by_access_token.assert_called_once_with("token")
        self.check_at(1060, reset=19000)
        self.assertIn("窗口已启动", self.service.status_for(self.row))
        self.assertTrue(self.path.exists())
        self.usage.request_refresh.assert_called_once()

    def test_no_repeat_across_checks_model_edits_or_restart(self):
        self.check()
        self.check()
        self.service.configure(True, "other-model")
        self.check()
        restarted = self.make_service()
        restarted.configure(True, "other-model")
        self.check(restarted)
        self.client.ping.assert_called_once()
        self.assertNotIn("token", self.path.read_text(encoding="utf-8"))
        self.assertNotIn("refresh", self.path.read_text(encoding="utf-8"))

    def test_disabled_stale_exhausted_and_active_windows_do_not_send(self):
        original = self.usage.item_for.return_value
        cases = [replace(original, observed_at=800), replace(original, windows=usage(used=2).windows),
                 replace(original, windows=usage(secondary_used=100).windows),
                 replace(original, windows=usage(used=1).windows), replace(original, windows=())]
        for item in cases:
            self.usage.item_for.return_value = item
            self.check()
        self.usage.item_for.return_value = original
        self.row.disabled = True
        self.check()
        self.row.disabled = False
        self.row.load_strategy = LoadStrategy.DISABLED
        self.check()
        self.row.load_strategy = LoadStrategy.NORMAL
        self.service.configure(False, DEFAULT_WARMUP_MODEL)
        self.check()
        self.client.ping.assert_not_called()

    def test_fresh_usage_can_cancel_cached_candidate(self):
        self.fetcher.fetch.side_effect = [usage(reset=9000, used=2)]
        self.check()
        self.client.ping.assert_not_called()

    def test_settings_or_token_changes_during_check_cancel_send(self):
        self.client.check_model.side_effect = lambda *_: self.service.configure(False, DEFAULT_WARMUP_MODEL)
        self.check()
        self.client.ping.assert_not_called()
        self.service.configure(True, DEFAULT_WARMUP_MODEL)
        self.fetcher.fetch.side_effect = None
        self.fetcher.fetch.return_value = usage()
        self.client.check_model.side_effect = lambda *_: setattr(self.row, "access_token", "rotated")
        self.check()
        self.client.ping.assert_not_called()

    def test_model_failure_backs_off_and_model_edit_allows_retry(self):
        self.client.check_model.side_effect = WarmupRejected("模型不可用")
        self.fetcher.fetch.side_effect = None
        self.fetcher.fetch.return_value = usage()
        self.check()
        self.check()
        self.client.check_model.assert_called_once()
        self.service.configure(True, "working-model")
        self.client.check_model.side_effect = None
        self.check()
        self.client.ping.assert_called_once()

    def test_ambiguous_timeout_is_not_retried_each_minute(self):
        self.client.ping.side_effect = TimeoutError()
        self.check()
        self.check()
        self.client.ping.assert_called_once()
        self.assertIn("结果不确定", self.service.status_for(self.row))

    def test_completed_ping_with_rolling_full_window_retries_after_backoff(self):
        self.fetcher.fetch.side_effect = lambda *_args, **_kwargs: usage(reset=time.time() + 18000)
        self.check()
        self.assertIn("等待窗口启动确认", self.service.status_for(self.row))
        self.assertEqual(self.service._attempts["account"]["until"], 1300)
        self.check()
        self.client.ping.assert_called_once()
        for now in (1060, 1120, 1180, 1240):
            self.check_at(now)
        self.usage.item_for.return_value = replace(
            self.usage.item_for.return_value, windows=usage(reset=19301).windows, observed_at=1301)
        self.fetcher.fetch.side_effect = [usage(reset=19301), usage(reset=19301, used=1)]
        with patch("app.services.quota_warmup_service.time.time", return_value=1301):
            self.service.check_once()
        self.assertEqual(self.client.ping.call_count, 2)
        self.check_at(1361, reset=19301)
        self.assertIn("窗口已启动", self.service.status_for(self.row))

    def test_stable_full_window_confirms_after_backoff_without_resending(self):
        self.fetcher.fetch.side_effect = [usage(), usage(reset=19000)]
        self.check()
        self.usage.item_for.return_value = replace(
            self.usage.item_for.return_value, windows=usage(reset=19000).windows, observed_at=1301)
        with patch("app.services.quota_warmup_service.time.time", return_value=1301):
            self.service.check_once()
        self.client.ping.assert_called_once()
        self.assertIn("窗口已启动", self.service.status_for(self.row))

    def test_timeout_can_retry_after_delayed_verification(self):
        self.fetcher.fetch.side_effect = lambda *_args, **_kwargs: usage(reset=time.time() + 18000)
        self.client.ping.side_effect = TimeoutError()
        self.check()
        self.usage.item_for.return_value = replace(
            self.usage.item_for.return_value, windows=usage(reset=19901).windows, observed_at=1901)
        with patch("app.services.quota_warmup_service.time.time", return_value=1901):
            self.service.check_once()
        self.client.ping.assert_called_once()
        self.assertIn("预热未生效", self.service.status_for(self.row))
        self.check_at(1961)
        self.assertEqual(self.client.ping.call_count, 2)

    def test_future_full_and_weekly_windows(self):
        self.assertTrue(self.service.ready(usage(reset=19000).windows, 1000))
        self.assertFalse(self.service.ready(usage(reset=19000, used=1).windows, 1000))
        self.assertTrue(self.service.ready(usage(reset=30000).windows, 1000))
        weekly = usage(reset=605800, duration=604800).windows
        self.assertTrue(self.service.ready(weekly, 1000))

    def test_first_candidate_records_baseline_and_repeated_cache_does_not_resend(self):
        self.service._attempts.clear()
        self.check()
        self.check()
        self.client.ping.assert_called_once()
        self.assertEqual(self.service._attempts["account"]["before_reset_at"], 19000)
        self.assertEqual(self.service._attempts["account"]["verify_after"], 1060)

    def test_full_quota_with_fixed_reset_never_sends(self):
        self.service._attempts.clear()
        self.service._observe("account", DEFAULT_WARMUP_MODEL, usage().windows, 940)
        self.check()
        self.check_at(1060, reset=19000)
        self.check_at(1301, reset=19000)
        self.client.ping.assert_not_called()
        self.assertIn("窗口已启动", self.service.status_for(self.row))

    def test_ninety_nine_percent_does_not_warmup(self):
        self.usage.item_for.return_value = replace(
            self.usage.item_for.return_value, windows=usage(used=1).windows)
        self.fetcher.fetch.side_effect = None
        self.fetcher.fetch.return_value = usage(used=1)
        self.check()
        self.client.ping.assert_not_called()

    def test_full_quota_with_expired_reset_is_not_missed(self):
        self.service._attempts.clear()
        self.check_at(1000, reset=990)
        self.check_at(1060, reset=990)
        self.client.ping.assert_called_once()

    def test_retries_continue_after_restart_and_model_change_until_started(self):
        self.fetcher.fetch.side_effect = lambda *_args, **_kwargs: usage(reset=time.time() + 18000)
        self.check()
        for now in range(1060, 1301, 60):
            self.check_at(now)
        self.assertEqual(self.client.ping.call_count, 2)
        restarted = self.make_service()
        restarted.configure(True, "other-model")
        for now in range(1360, 1961, 60):
            self.check_at(now, service=restarted)
        self.assertEqual(self.client.ping.call_count, 4)
        self.assertEqual(restarted._attempts["account"]["phase"], "retry")
        self.fetcher.fetch.side_effect = None
        self.fetcher.fetch.return_value = usage(reset=19900)
        for now in range(2020, 2501, 60):
            self.check_at(now, reset=19900, service=restarted)
        self.assertEqual(self.client.ping.call_count, 4)
        self.assertIn("窗口已启动", restarted.status_for(self.row))

    def test_timeout_retries_every_five_minutes_if_window_keeps_sliding(self):
        self.fetcher.fetch.side_effect = lambda *_args, **_kwargs: usage(reset=time.time() + 18000)
        self.client.ping.side_effect = TimeoutError()
        self.check()
        restarted = self.make_service()
        restarted.configure(True, "other-model")
        for now in range(1060, 1601, 60):
            self.check_at(now, service=restarted)
        self.assertEqual(self.client.ping.call_count, 3)
        self.assertIn("结果不确定", restarted.status_for(self.row))

    def test_user_activity_confirms_start_without_requiring_quota_drop(self):
        self.fetcher.fetch.side_effect = [usage(), usage()]
        self.check()
        self.check_at(1060, reset=19000)
        self.check_at(1300, reset=19000)
        self.client.ping.assert_called_once()
        self.assertIn("窗口已启动", self.service.status_for(self.row))

    def test_new_cycle_gets_new_budget_only_after_confirmed_window_expires(self):
        self.check()
        self.check_at(1060, reset=19000)
        self.fetcher.fetch.side_effect = [usage(reset=37001), usage(reset=37001)]
        self.check_at(19001)
        self.assertEqual(self.client.ping.call_count, 2)
        self.check_at(19061)
        self.assertEqual(self.client.ping.call_count, 2)
        state = self.service._attempts["account"]
        self.assertEqual(state["cycle"], 2)
        self.assertEqual(state["count"], 1)

    def test_weekly_first_window_uses_same_observation_rules(self):
        self.service._attempts.clear()
        for now in (1000, 1060):
            result = usage(reset=now + 604800, duration=604800)
            self.usage.item_for.return_value = replace(
                self.usage.item_for.return_value, windows=result.windows, observed_at=now)
            self.fetcher.fetch.side_effect = None
            self.fetcher.fetch.return_value = result
            with patch("app.services.quota_warmup_service.time.time", return_value=now):
                self.service.check_once()
        self.client.ping.assert_called_once()

    def test_full_quota_after_observation_gap_still_verifies_after_ping(self):
        self.fetcher.fetch.side_effect = [usage(reset=19600), usage(reset=19600)]
        self.check_at(1600)
        self.client.ping.assert_called_once()
        self.check_at(1660)
        self.client.ping.assert_called_once()
        self.assertIn("窗口已启动", self.service.status_for(self.row))

    def test_retry_waits_five_minutes_and_stops_if_quota_drops(self):
        self.fetcher.fetch.side_effect = lambda *_args, **_kwargs: usage(reset=time.time() + 18000)
        self.check()
        self.check_at(1060)
        self.service.configure(True, "other-model")
        self.check_at(1299)
        self.client.ping.assert_called_once()
        self.check_at(1300, used=1)
        self.client.ping.assert_called_once()

    def test_old_exhausted_record_can_retry_without_resetting_count(self):
        self.service._attempts["account"].update(
            phase="exhausted", count=2, until=1000, observation={})
        self.check()
        self.client.ping.assert_called_once()
        self.assertEqual(self.service._attempts["account"]["count"], 3)

    def test_early_reset_of_active_account_is_not_missed(self):
        self.check()
        self.check_at(1060, reset=19000)
        self.fetcher.fetch.side_effect = [usage(reset=20000)]
        self.check_at(2000, reset=20000)
        self.assertEqual(self.client.ping.call_count, 2)
        self.assertEqual(self.service._attempts["account"]["cycle"], 2)

    def test_legacy_record_without_baseline_recovers_after_five_minutes(self):
        self.service._attempts["account"] = {
            "model": DEFAULT_WARMUP_MODEL, "until": 19000, "attempted": True}
        self.fetcher.fetch.side_effect = lambda *_args, **_kwargs: usage(reset=time.time() + 18000)
        self.check()
        self.check_at(1299)
        self.client.ping.assert_not_called()
        self.check_at(1300)
        self.client.ping.assert_called_once()

    def test_verification_failures_do_not_permanently_block_compensation(self):
        self.check()
        self.fetcher.fetch.side_effect = None
        self.fetcher.fetch.return_value = UsageFetchResult(message="network failure")
        for now in range(1060, 1301, 60):
            self.check_at(now)
        self.client.ping.assert_called_once()
        self.assertEqual(self.service._attempts["account"]["phase"], "retry")
        self.fetcher.fetch.return_value = usage(reset=19360)
        self.check_at(1360)
        self.assertEqual(self.client.ping.call_count, 2)

    def test_timeout_that_started_window_does_not_need_compensation(self):
        self.client.ping.side_effect = TimeoutError()
        self.fetcher.fetch.side_effect = [usage(), usage()]
        self.check()
        self.check_at(1060)
        self.check_at(1360, reset=19000)
        self.client.ping.assert_called_once()
        self.assertIn("窗口已启动", self.service.status_for(self.row))

    def test_legacy_attempt_is_not_blindly_repeated(self):
        self.service._attempts["account"] = {
            "model": DEFAULT_WARMUP_MODEL, "until": 19000, "attempted": True,
            "status": "预热完成，窗口已启动"}
        self.check()
        self.check_at(1060)
        self.client.ping.assert_not_called()
        self.assertIn("缺少验证基准", self.service.status_for(self.row))

    def test_persistence_failure_prevents_ping(self):
        with patch.object(Path, "replace", side_effect=OSError("disk unavailable")):
            with self.assertRaises(OSError):
                self.check()
        self.client.ping.assert_not_called()

    def test_quota_failure_after_completed_request_does_not_clear_budget(self):
        self.fetcher.fetch.side_effect = [usage(), UsageFetchResult(message="request failed")]
        self.check()
        original = self.usage.item_for.return_value
        self.usage.item_for.return_value = replace(original, windows=())
        with patch("app.services.quota_warmup_service.time.time", return_value=1060):
            self.service.check_once()
        self.assertEqual(self.service._attempts["account"]["phase"], "waiting")
        self.assertEqual(self.service._attempts["account"]["count"], 1)
        self.client.ping.assert_called_once()

    def test_disabled_during_model_check_cancels_ping(self):
        self.client.check_model.side_effect = lambda *_: setattr(self.row, "disabled", True)
        self.check()
        self.client.ping.assert_not_called()

    def test_secondary_window_zero_blocks_send(self):
        self.usage.item_for.return_value = replace(
            self.usage.item_for.return_value, windows=usage(secondary_used=100).windows)
        self.check()
        self.client.ping.assert_not_called()

    def test_send_budget_is_persisted_before_network_call(self):
        def verify(*_args):
            persisted = self.make_service()._attempts["account"]
            self.assertEqual(persisted["phase"], "uncertain")
            self.assertEqual(persisted["count"], 1)
            self.assertEqual(persisted["last_sent_at"], 1000)
        self.client.ping.side_effect = verify
        self.check()
        self.client.ping.assert_called_once()

    def test_small_reset_jitter_confirms_active_full_window(self):
        self.service._attempts.clear()
        self.service._observe("account", DEFAULT_WARMUP_MODEL, usage().windows, 940)
        self.check()
        self.check_at(1060, reset=19005)
        self.client.ping.assert_not_called()
        self.assertIn("窗口已启动", self.service.status_for(self.row))

    def test_verification_waits_sixty_seconds_and_fetches_independently(self):
        self.fetcher.fetch.side_effect = [usage(), usage()]
        self.check()
        self.check_at(1059)
        self.assertEqual(self.fetcher.fetch.call_count, 1)
        # 缓存即使错误地向后滑动，也必须以独立查询返回的重置时间为准。
        self.check_at(1060, reset=19060)
        self.assertEqual(self.fetcher.fetch.call_count, 2)
        self.assertEqual(self.service._attempts["account"]["phase"], "active")
        self.client.ping.assert_called_once()

    def test_changed_reset_after_one_minute_is_failure_not_success(self):
        self.fetcher.fetch.side_effect = [usage(), usage(reset=19060)]
        self.check()
        self.check_at(1060, reset=19000)
        self.assertEqual(self.service._attempts["account"]["phase"], "retry")
        self.assertIn("预热未生效", self.service.status_for(self.row))
        self.client.ping.assert_called_once()

    def test_restart_keeps_pre_ping_baseline_for_verification(self):
        self.fetcher.fetch.side_effect = [usage(), usage(reset=19060)]
        self.check()
        restarted = self.make_service()
        restarted.configure(True, "other-model")
        self.check_at(1060, service=restarted)
        self.assertEqual(restarted._attempts["account"]["phase"], "retry")
        self.client.ping.assert_called_once()

    def test_duration_change_is_uncertain_and_does_not_allow_retry(self):
        self.fetcher.fetch.side_effect = [usage(), usage(reset=605860, duration=604800)]
        self.check()
        self.check_at(1060)
        self.assertEqual(self.service._attempts["account"]["phase"], "uncertain")
        self.assertIn("窗口变化异常", self.service.status_for(self.row))
        self.client.ping.assert_called_once()

    def test_one_minute_delay_starts_after_ping_completes(self):
        clock = [1000]
        self.client.ping.side_effect = lambda *_args: clock.__setitem__(0, 1100)
        with patch("app.services.quota_warmup_service.time.time", side_effect=lambda: clock[0]):
            self.service.check_once()
        self.assertEqual(self.service._attempts["account"]["verify_after"], 1160)
        self.check_at(1159)
        self.assertEqual(self.fetcher.fetch.call_count, 1)
        self.check_at(1160)
        self.assertEqual(self.service._attempts["account"]["phase"], "active")

    def test_missing_or_invalid_window_data_is_not_guessed(self):
        data = usage()
        del data.raw["rate_limit"]["primary_window"]["limit_window_seconds"]
        self.assertEqual(data.windows, ())
        for value in (True, float("nan"), "18000", -1):
            data = usage(duration=value)
            self.assertEqual(data.windows, ())

    def test_config_roundtrip_and_old_config_default(self):
        config = AppConfigService(Path(self.temp.name) / "config.json")
        config.config_path.write_text('{"quota_warmup": true}', encoding="utf-8")
        self.assertEqual(config.load().quota_warmup_model, DEFAULT_WARMUP_MODEL)
        config.save(AppConfig(quota_warmup=True, quota_warmup_model="my-model"))
        self.assertEqual(config.load().quota_warmup_model, "my-model")

    def test_ui_entry_enable_save_and_disable(self):
        from app.ui.proxy_window import ProxyWindow
        from tkinter import ttk

        root = tk.Tk()
        root.withdraw()
        self.addCleanup(root.destroy)
        window = ProxyWindow.__new__(ProxyWindow)
        window.quota_warmup_service = self.service
        window.quota_warmup_var = tk.BooleanVar(root, True)
        window.quota_warmup_model_var = tk.StringVar(root, "  custom-model  ")
        window._quota_warmup_model_entry = ttk.Entry(root, textvariable=window.quota_warmup_model_var)
        window._is_relay_active = Mock(return_value=False)
        window._persist_config = Mock()
        window._recompute_auto_load_target = Mock()
        window._on_quota_warmup_model_edited()
        self.assertEqual(window.quota_warmup_model_var.get(), "custom-model")
        self.assertEqual(str(window._quota_warmup_model_entry["state"]), "normal")
        window.quota_warmup_var.set(False)
        window._on_quota_warmup_toggled()
        self.assertEqual(str(window._quota_warmup_model_entry["state"]), "disabled")
        self.assertEqual(window.quota_warmup_model_var.get(), "custom-model")
        window._persist_config.assert_called()


class WarmupClientTests(unittest.TestCase):
    def test_post_payload_headers_proxy_and_completed_sse(self):
        client = CodexWarmupClient()
        response = io.BytesIO(b'data: {"type":"response.created"}\n\ndata: {"type":"response.completed"}\n\n')
        opener = Mock()
        opener.open.return_value = response
        with patch("app.utils.codex_warmup_client.build_opener", return_value=opener) as build:
            client.ping("account-token", "account-id", "127.0.0.1:1088", "gpt-6-luna", Event())
        request = opener.open.call_args.args[0]
        self.assertEqual(request.get_header("Authorization"), "Bearer account-token")
        self.assertEqual(request.get_header("Chatgpt-account-id"), "account-id")
        payload = json.loads(request.data)
        self.assertEqual(payload["model"], "gpt-6-luna")
        self.assertEqual(payload["input"][0]["content"][0]["text"], "ping")
        self.assertEqual(payload["tools"], [])
        self.assertFalse(payload["store"])
        self.assertEqual(build.call_args.args[0].proxies["https"], "http://127.0.0.1:1088")

    def test_incomplete_stream_is_uncertain_and_unsupported_model_rejected(self):
        client = CodexWarmupClient()
        with patch.object(client, "_open", return_value=io.BytesIO(b'data: {"type":"response.failed"}\n')):
            with self.assertRaises(TimeoutError):
                client.ping("t", "a", "", "m", Event())
        with patch.object(client, "_open", return_value=io.BytesIO(b'{"models":[]}')):
            with self.assertRaises(WarmupRejected):
                client.check_model("t", "a", "", "m")


if __name__ == "__main__":
    unittest.main()
