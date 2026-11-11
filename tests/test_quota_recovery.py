import asyncio
import gc
import json
import queue
import threading
import time
import tkinter as tk
import unittest
from collections import Counter, OrderedDict
from concurrent.futures import Future
from types import SimpleNamespace
from unittest.mock import Mock
from tkinter import ttk

from app.models import CREDENTIAL_TYPE_CODEX_AUTH
from app.services.auth_cooldown_service import AuthCooldownService
from app.services.auth_sync_service import AuthFileRow
from app.services.auth_usage_service import AuthUsageService
from app.services.app_server_mqtt_service import AppServerMqttService
from app.services.quota_recovery_service import QuotaRecoveryService
from app.ui.proxy_window import ProxyWindow
from app.utils.chatgpt_usage_fetcher import UsageFetchResult


def failed_turn(thread_id="thread-1", turn_id="turn-1", code="usageLimitExceeded"):
    return {"method": "turn/completed", "params": {
        "threadId": thread_id,
        "turn": {"id": turn_id, "status": "failed", "error": {
            "message": "额度不足", "codexErrorInfo": code,
        }},
    }}


class QuotaRecoveryAcceptance(unittest.TestCase):
    def setUp(self):
        self.correct = Mock()
        self.send = Mock()
        self.service = QuotaRecoveryService(self.correct, self.send, Mock())
        self.service.set_enabled(True)

    def test_quota_error_triggers_correction_then_continues_once(self):
        event = failed_turn()
        self.service.observe(event)
        self.service.observe(event)
        self.correct.assert_called_once()
        self.send.assert_not_called()
        self.service.correction_finished()
        self.service.correction_finished()
        self.send.assert_called_once_with("thread-1")

    def test_manual_continue_or_disabled_switch_cancels_pending(self):
        self.service.observe(failed_turn())
        self.service.observe({"method": "turn/start", "params": {"threadId": "thread-1"}}, "input")
        self.service.observe(failed_turn("thread-2", "turn-2"))
        self.service.set_enabled(False)
        self.service.correction_finished()
        self.send.assert_not_called()

    def test_still_exhausted_does_not_loop(self):
        self.service.observe(failed_turn())
        self.service.correction_finished()
        self.service.observe(failed_turn(turn_id="turn-2"))
        self.service.correction_finished()
        self.send.assert_called_once_with("thread-1")


class AppServerBridgeAcceptance(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.service = AppServerMqttService()
        self.service.attached = self.service.writer_ready = True
        self.events = []
        self.service.set_protocol_callback(lambda message, direction: self.events.append((message, direction)))
        self.chunks = queue.Queue()
        self.stats = {"bytes": 0, "peak": 0, "wait": 0}
        self.sequence = Counter()
        self.writes = []
        self.raise_write = False
        self.capture_ready = asyncio.Event()
        self.loop = asyncio.get_running_loop()
        self.service._publish_peak = 0.0
        self.service._queue_peak = 0
        self.writer = SimpleNamespace(ready=True, write=self.write)

        class Client:
            session = SimpleNamespace(transitions=SimpleNamespace(is_connected=lambda: True))

            async def publish(self, *args, **kwargs):
                pass

            async def deliver_message(self):
                await asyncio.Future()

        client = Client()
        self.task = asyncio.create_task(self.service._bridge(
            client, self.writer, "codex/test", self.chunks, threading.Event(), threading.Event(),
            queue.SimpleQueue(), SimpleNamespace(running=True), OrderedDict(),
            SimpleNamespace(pid=1), 1, Mock(), self.stats, self.capture_ready, self.sequence,
        ))
        await asyncio.sleep(0.05)

    async def asyncTearDown(self):
        self.service._stop.set()
        await asyncio.wait_for(self.task, 2)

    def inject(self, message, direction="output"):
        data = (json.dumps(message, ensure_ascii=False) + "\n").encode()
        seq = self.sequence[direction]
        self.sequence[direction] += 1
        self.stats["bytes"] += len(data)
        self.chunks.put(({"kind": "bytes", "direction": direction}, data, time.monotonic(), direction, seq))
        self.capture_ready.set()

    def write(self, payload):
        if self.raise_write:
            raise RuntimeError("管道投递状态未知")
        message = json.loads(payload)
        self.writes.append(message)
        if message["method"] == "thread/turns/list":
            result = {"data": [{"id": "turn-1", "status": "failed"}]}
        else:
            result = {"turn": {"id": "turn-2"}}
        self.loop.call_soon_threadsafe(self.inject, {"id": message["id"], "result": result})

    async def test_continue_uses_confirmed_writer(self):
        future = self.service.request("turn/start", {"threadId": "thread-1", "input": []})
        result = await asyncio.wait_for(asyncio.wrap_future(future), 2)
        self.assertIn("result", result)
        self.assertEqual(len(self.writes), 1)

    async def test_toggle_guard_and_unconfirmed_writer_block_submission(self):
        future = self.service.request("thread/turns/list", {"threadId": "thread-1"}, guard=lambda: False)
        self.assertIn("error", await asyncio.wait_for(asyncio.wrap_future(future), 2))
        self.service.writer_ready = False
        self.assertIn("error", self.service.request("turn/start", {}).result())
        self.assertFalse(self.writes)

    async def test_write_failure_marks_delivery_unknown(self):
        self.raise_write = True
        future = self.service.request("thread/read", {"threadId": "thread-1"})
        result = await asyncio.wait_for(asyncio.wrap_future(future), 2)
        self.assertTrue(result["deliveryUnknown"])

class CorrectTrafficUiAcceptance(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.window = window = ProxyWindow.__new__(ProxyWindow)
        window.root = self.root
        window._closing = False
        window._ui_queue = queue.Queue()
        window._correct_traffic_button = ttk.Button(self.root, text="矫正流量")
        window._correct_traffic_refreshing = False
        window._correct_traffic_after_id = None
        window._manual_proxy_kill_pending = False
        window._auto_load_lock = threading.Lock()
        window._auto_load_enabled = True
        window._active_credential_type = CREDENTIAL_TYPE_CODEX_AUTH
        window._auto_load_target_refresh_token = "refresh-a"
        window._auto_load_target_access_token = "token-a"
        window._auto_load_target_account_id = "account-a"
        window._proxy_kill_pending_reason = ""
        window._auth_cooldown = AuthCooldownService()
        window.auto_retry_var = tk.BooleanVar(self.root, True)
        window.quota_warmup_var = tk.BooleanVar(self.root, False)
        window.service = SimpleNamespace(process=SimpleNamespace(poll=lambda: None))
        window.refresh_auth_files = Mock()
        window._sync_proxy_kill_pending_with_target = Mock()
        window._update_quota_drop_state = Mock()
        rows = [AuthFileRow("account-a", "refresh-a", "", quota="90%, 90%", access_token="token-a"),
                AuthFileRow("account-b", "refresh-b", "", quota="50%, 50%", access_token="token-b")]
        self.rows = rows

        def update_cache(quotas, *args):
            for row in rows:
                row.quota = quotas.get(row.refresh_token, "")

        window.auth_sync_service = SimpleNamespace(list_auth_rows=lambda: rows, update_usage_cache=update_cache)
        self.fetcher = Mock()
        self.fetcher.fetch.side_effect = lambda token, account: UsageFetchResult(
            quota="0%, 0%" if token == "token-a" else "80%, 90%",
            quota_refresh_time_5h="2026-11-13T00:00:00+09:00")
        window.auth_usage_service = AuthUsageService(window.auth_sync_service, self.fetcher)
        self.send = Mock()
        window.quota_recovery_service = QuotaRecoveryService(window.correct_traffic, self.send, Mock())
        window.quota_recovery_service.set_enabled(True)
        self.root.after(0, window._drain_ui_queue)

    def tearDown(self):
        self.wait_refresh()
        self.window._closing = True
        for timer in self.root.tk.call("after", "info"):
            self.root.after_cancel(timer)
        self.window.auto_retry_var = None
        self.window.quota_warmup_var = None
        self.root.destroy()
        self.window._correct_traffic_button = None
        self.window.root = None
        self.root = None
        gc.collect()

    def wait_refresh(self):
        deadline = time.monotonic() + 3
        while self.window._correct_traffic_refreshing and time.monotonic() < deadline:
            self.root.update()
            time.sleep(0.005)
        self.assertFalse(self.window._correct_traffic_refreshing)

    def test_uses_existing_countdown_without_waiting_for_disconnect_ack(self):
        self.window.quota_recovery_service.observe(failed_turn())
        self.wait_refresh()
        self.assertEqual(self.fetcher.fetch.call_count, 2)
        self.assertTrue(self.window._manual_proxy_kill_pending)
        self.assertEqual(self.window._correct_traffic_button["text"], "重置中 5")
        self.send.assert_not_called()
        self.root.after(5500, self.root.quit)
        self.root.mainloop()
        self.send.assert_called_once_with("thread-1")
        self.assertEqual(str(self.window._correct_traffic_button["state"]), "normal")

    def test_manual_correction_does_not_send_continue(self):
        self.window.correct_traffic()
        self.wait_refresh()
        self.window.root.after_cancel(self.window._correct_traffic_after_id)
        self.window._update_correct_traffic_countdown(0)
        self.send.assert_not_called()

    def test_resume_submits_empty_input_without_a_user_message(self):
        reply = Future()
        reply.set_result({"result": {"turn": {"id": "resumed-turn"}}})
        self.window._app_server_mqtt = Mock()
        self.window._app_server_mqtt.request.return_value = reply
        self.window._log_quota_recovery = Mock()
        self.window._continue_after_quota_error("thread-1")
        args, _options = self.window._app_server_mqtt.request.call_args
        self.assertEqual(args, ("turn/start", {"threadId": "thread-1", "input": []}))


if __name__ == "__main__":
    unittest.main()
