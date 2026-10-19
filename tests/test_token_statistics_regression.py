import io
import json
import socket
import tempfile
import time
import tkinter as tk
import unittest
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.services.proxy_logger_addon import ProxyLoggerAddon
from app.services.response_payload_reader import PayloadReader, ResponsePayloadBuffer
from app.services.response_token_speed_service import ResponseTokenSpeedService
from app.services.response_usage_ledger import ResponseUsageLedger
from app.services.token_history_service import TokenHistoryService
from app.ui.proxy_window import ProxyWindow
from app.ui.token_history_window import format_token_totals
from app.models import CredentialType, LoadStrategy, ProxyControlMessage, ResetCreditResult
from app.services.app_config_service import AppConfig, AppConfigService
from app.models import ResetCredit, ResetCreditInfo
from app.services.auth_sync_service import AuthFileRow
from app.services.auth_reset_credit_service import AuthResetCreditService
from app.utils.chatgpt_reset_credit_fetcher import ChatGPTResetCreditFetcher


class TokenStatisticsRegressionTests(unittest.TestCase):
    def setUp(self):
        self.service = ResponseTokenSpeedService(Mock())
        self.encoding = self.service._get_encoding()
        self.now = time.monotonic()

    def tearDown(self):
        for key in list(self.service._streams):
            self.service._close_stream(key)
        if self.service._ledger:
            self.service._ledger.close()

    def message(self, event, key="ws:one"):
        raw = json.dumps(event, ensure_ascii=False).encode()
        for offset in range(0, len(raw), 65536):
            self.service._handle("data", key, raw[offset:offset + 65536], "", self.now)
        self.service._handle("message_end", key, b"", "", self.now)

    @staticmethod
    def item(text="hello", identity="item"):
        return {"type": "message", "id": identity, "content": [{"type": "output_text", "text": text}]}

    def completed(self, identity="response", text="hello", usage=None, key="ws:one"):
        response = {"id": identity, "output": [self.item(text)]}
        if usage is not None:
            response["usage"] = usage
        self.message({"type": "response.completed", "response": response}, key)

    def tokens(self):
        return sum(count for _, count in self.service._buckets)

    def test_large_tool_payload_sse_and_websocket(self):
        text = "print('中文');\n" * 60000
        payload = {"type": "response.output_item.done", "item": {"id": "tool", "type": "custom_tool_call", "input": text}}
        self.message(payload)
        count = self.tokens()
        self.assertGreater(count, 100000)
        raw = ("event: response.output_item.done\ndata: " + json.dumps(payload, ensure_ascii=False) + "\n\n").encode()
        for offset in range(0, len(raw), 65536):
            self.service._handle("data", "sse:large", raw[offset:offset + 65536], "", self.now)
        self.assertEqual(self.tokens(), count * 2)
        self.assertFalse(self.service._incomplete)

    def test_spool_and_skip_large_image_keep_usage(self):
        events, issues = [], []
        buffer = ResponsePayloadBuffer(lambda source, now: events.append(PayloadReader(source).read()), issues.append)
        buffer.feed('{"type":"response.completed","response":{"id":"image","output":[{"type":"image_generation_call","result":"', 0)
        for _ in range(128):
            buffer.feed("A" * 65536, 0)
        self.assertTrue(buffer.file._rolled)
        buffer.feed('"}],"usage":{"input_tokens":10,"output_tokens":200,"total_tokens":210}}}', 0)
        buffer.end(0)
        self.assertEqual(events[0]["response"]["usage"]["output_tokens"], 200)
        self.assertNotIn("result", events[0]["response"]["output"][0])
        self.assertFalse(issues)
        self.assertIsNone(buffer.file)

    def test_large_capture_does_not_fill_queue(self):
        self.service.observe("ws:large", b"x" * (20 * 1024 * 1024))
        self.assertEqual(self.service._queue.qsize(), 1)
        self.assertFalse(self.service._overflow.is_set())
        queued = self.service._queue.get_nowait()
        self.assertEqual(queued[0], "file")
        self.service._release_capture(queued[2])

    def test_usage_fields_and_replay_across_connections(self):
        usage = {"input_tokens":1000, "output_tokens":100, "total_tokens":1100,
                 "input_tokens_details":{"cached_tokens":800,"cache_write_tokens":50},
                 "output_tokens_details":{"reasoning_tokens":90}}
        self.completed(usage=usage)
        self.completed(usage=usage, key="ws:reconnected")
        self.assertEqual(self.tokens(), 1)
        records = self.service._ledger.snapshot()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["usage"], {"input":1000,"output":100,"total":1100,
                         "cached_input":800,"cache_write_input":50,"reasoning_output":90})

    def test_usage_without_visible_compaction_output(self):
        self.message({"type":"response.completed","response":{"id":"compact", "output":[{"type":"compaction","encrypted_content":"opaque"}],
                      "usage":{"input_tokens":200,"output_tokens":30,"total_tokens":230}}})
        self.assertEqual(self.tokens(), 0)
        self.assertEqual(self.service._ledger.snapshot()[0]["usage"]["output"], 30)

    def test_long_response_dedup_and_finished_ids(self):
        for index in range(700):
            self.message({"type":"response.output_item.done","output_index":index,"item":self.item(identity=str(index))})
        self.message({"type":"response.completed","response":{"id":"long","output":[self.item(identity=str(i)) for i in range(700)]}})
        self.assertEqual(self.tokens(), 700)
        for index in range(20):
            self.completed(identity=str(index))
        self.completed(identity="0")
        self.assertEqual(self.tokens(), 720)

    def test_anonymous_sequential_responses(self):
        for _ in range(2):
            self.message({"type":"response.output_text.done","item_id":"same","text":"hello"})
            self.message({"type":"response.completed","response":{"output":[self.item(identity="same")]}})
        self.assertEqual(self.tokens(), 2)

    def test_current_events_thread_isolation_and_reasoning_text(self):
        for thread in ("a", "b"):
            self.message({"type":"agent_message_content_delta","thread_id":thread,"turn_id":"turn","item_id":"same","delta":"hello"})
            self.message({"method":"item/completed","params":{"threadId":thread,"turnId":"turn", "item":{"id":"same","type":"agentMessage","text":"hello"}}})
        for kind in ("reasoning_content_delta", "reasoning_raw_content_delta", "plan_delta"):
            self.message({"type":kind,"thread_id":"events","turn_id":"turn","item_id":kind,"delta":"hello"})
        self.message({"type":"response.output_item.done","response_id":"reasoning","item":{"id":"reason","type":"reasoning","content":[{"type":"text","text":"hello"}]}})
        self.assertEqual(self.tokens(), 6)

    def test_content_part_and_audio_transcript_final_dedup(self):
        self.message({"type":"response.content_part.done","item_id":"item","content_index":0,"part":{"type":"output_text","text":"hello"}})
        self.completed()
        self.message({"type":"response.output_audio_transcript.delta","response_id":"audio","item_id":"transcript","delta":"world"})
        self.message({"type":"response.output_audio_transcript.done","response_id":"audio","item_id":"transcript","transcript":"world"})
        self.assertEqual(self.tokens(), 2)

    def test_chat_completion_multiple_tools_and_usage(self):
        self.message({"id":"chat","choices":[{"index":0,"message":{"tool_calls":[
            {"function":{"name":"one","arguments":"hello"}}, {"function":{"name":"two","arguments":"world"}}]}}],
            "usage":{"prompt_tokens":10,"completion_tokens":2,"total_tokens":12}})
        self.assertEqual(self.tokens(), 2)
        self.assertEqual(self.service._ledger.snapshot()[0]["usage"]["output"], 2)

    def test_raw_completion_usage_and_cumulative_notification_not_added(self):
        self.message({"method":"rawResponse/completed","params":{"responseId":"raw","tokenUsage":{"inputTokens":10,"outputTokens":3,"totalTokens":13}}})
        self.message({"method":"thread/tokenUsage/updated","params":{"threadId":"thread","turnId":"turn","tokenUsage":{"total":{"inputTokens":1000,"outputTokens":300}}}})
        self.assertEqual(len(self.service._ledger.snapshot()), 1)
        self.assertEqual(self.service._ledger.snapshot()[0]["usage"]["total"], 13)

    def test_event_first_sse_without_content_type_and_invalid_recovery(self):
        self.service._handle("data", "http:sniff", b'event: delta\ndata: {"type":"response.output_text.delta","delta":"hello"}\n\n', "", self.now)
        self.service._handle("end", "http:sniff", b"", "", self.now)
        self.assertEqual(self.tokens(), 1)
        self.service._handle("data", "ws:bad", b"not json", "", self.now)
        self.service._handle("message_end", "ws:bad", b"", "", self.now)
        self.completed(identity="after-bad", key="ws:bad")
        self.assertEqual(self.tokens(), 2)
        self.assertTrue(self.service._incomplete)

    def test_report_retry_preserves_newer_snapshot(self):
        self.service._report = Mock(side_effect=[False, True, True])
        self.message({"type":"response.output_text.delta","response_id":"r","item_id":"item","delta":"hello"})
        self.assertFalse(self.service._publish())
        self.message({"type":"response.output_text.delta","response_id":"r","item_id":"item","delta":" world"})
        self.assertTrue(self.service._publish())
        self.assertTrue(self.service._ledger.dirty)
        self.assertTrue(self.service._publish())
        self.assertEqual(self.service._report.call_args[0][0]["records"][0]["estimate"], 2)
        self.assertFalse(self.service._ledger.dirty)

    def test_history_actual_usage_corrections_restart_and_incomplete(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.sqlite3"
            service = TokenHistoryService(path)
            now = int(time.time())
            report = {"session":"session","records":[{"id":"r","timestamp":now,"estimate":100,"usage":None}]}
            try:
                service.record_report(report).result(timeout=3)
                service.record_report(report).result(timeout=3)
                totals = service.read_totals_async(now-10,now+10).result(timeout=3)
                self.assertEqual(totals["estimate"],100)
                report["records"][0]["estimate"] = 1
                report["records"][0]["usage"] = {"input":10,"output":30,"total":40,"reasoning_output":20}
                report["incomplete"] = ["测试缺口"]
                service.record_report(report).result(timeout=3)
                service.record_report({"session":"session","records":[]}).result(timeout=3)
                totals = service.read_totals_async(now-10,now+10).result(timeout=3)
                self.assertEqual((totals["output"],totals["estimate"]),(30,0))
                self.assertIn("测试缺口", totals["incomplete"])
                self.assertEqual(format_token_totals(totals), "输入 10（缓存 0） · 输出 30（推理 20） · 总计 40")
            finally:
                service.stop()
            service = TokenHistoryService(path)
            try:
                totals = service.read_totals_async(now-10,now+10).result(timeout=3)
                self.assertEqual(totals["total"],40)
                self.assertIn("测试缺口",totals["incomplete"])
            finally:
                service.stop()

    def test_control_socket_fragmented_report_durable_ack(self):
        with tempfile.TemporaryDirectory() as directory:
            history = TokenHistoryService(Path(directory)/"history.sqlite3")
            server = socket.socket()
            server.bind(("127.0.0.1",0));server.listen();server.settimeout(.1)
            window = ProxyWindow.__new__(ProxyWindow)
            window._auto_load_control_stop = Event()
            window._token_history_service = history
            window._post_ui = Mock()
            thread = Thread(target=window._run_auto_load_control_server,args=(server,),daemon=True)
            thread.start()
            now = int(time.time())
            report = {"session":"socket","records":[{"id":str(i),"timestamp":now,"estimate":1,"usage":None} for i in range(64)]}
            raw = ("TOKEN_SPEED " + json.dumps(report) + "\n").encode()
            try:
                with socket.create_connection(server.getsockname(),timeout=1) as conn:
                    for offset in range(0,len(raw),7):conn.sendall(raw[offset:offset+7])
                    self.assertEqual(conn.recv(256).strip(),b"OK")
                totals = history.read_totals_async(now-10,now+10).result(timeout=3)
                self.assertEqual(totals["estimate"],64)
            finally:
                window._auto_load_control_stop.set();server.close();thread.join(timeout=2);history.stop()

    def test_relay_speed_label_without_changing_network(self):
        window = ProxyWindow.__new__(ProxyWindow)
        window._is_relay_active = Mock(return_value=True)
        window._auto_load_check = None;window._quota_warmup_check = None
        window._token_speed_var = Mock();window._set_auto_load_target=Mock();window._clear_proxy_kill_pending=Mock()
        window._refresh_credential_mode_state()
        self.assertEqual("0.0 token/s",window._token_speed_var.set.call_args[0][0])

    def test_outbox_survives_proxy_restart_before_ack(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/"outbox.sqlite3"
            ledger = ResponseUsageLedger(path)
            ledger.usage("pending", {"input_tokens":10,"output_tokens":20,"total_tokens":30}, self.now)
            ledger.close()
            ledger = ResponseUsageLedger(path)
            try:
                self.assertTrue(ledger.dirty)
                self.assertEqual(ledger.snapshot()[0]["usage"]["total"],30)
                ledger.acknowledge(ledger.snapshot())
                self.assertFalse(ledger.dirty)
            finally:
                ledger.close()

    def test_empty_choices_final_usage_and_notification_coverage(self):
        self.message({"id":"chat","choices":[{"index":0,"delta":{"content":"hello"},"finish_reason":"stop"}]})
        self.message({"id":"chat","choices":[],"usage":{"prompt_tokens":10,"completion_tokens":3,"total_tokens":13}})
        self.assertEqual(self.service._ledger.snapshot()[0]["usage"]["output"],3)
        common={"threadId":"thread","turnId":"turn","itemId":"item"}
        self.message({"method":"item/agentMessage/delta","params":{**common,"delta":"hello"}})
        self.message({"method":"rawResponse/completed","params":{**common,"responseId":"raw","tokenUsage":{"inputTokens":20,"outputTokens":5,"totalTokens":25}}})
        records={record["id"]:record for record in self.service._ledger.snapshot()}
        self.assertEqual(records["thread:turn"]["estimate"],0)
        self.assertEqual(records["raw"]["usage"]["output"],5)

    def test_oversized_text_retains_usage_and_parser_chunk_boundaries(self):
        reader=PayloadReader(io.StringIO('{"type":"response.completed","response":{"output":[{"type":"custom_tool_call","input":"' + 'x'*(16*1024*1024+100) + '"}],"usage":{"input_tokens":10,"output_tokens":20,"total_tokens":30}}}'))
        payload=reader.read()
        self.assertEqual(payload["response"]["usage"]["output_tokens"],20)
        self.assertIsNone(payload["response"]["output"][0]["input"])
        self.assertTrue(reader.issues)
        # 中文、引号和转义恰好跨 64 KiB 边界。
        original={"text":"a"*65522+'中文\\\"\n','result':{'usage':{'input_tokens':1,'output_tokens':2}}}
        self.assertEqual(PayloadReader(io.StringIO(json.dumps(original,ensure_ascii=False))).read(),original)

    def test_negative_reconciliation_changes_history_without_speed_integration(self):
        self.message({"type":"response.output_text.delta","response_id":"r","item_id":"item","delta":"hello "*20})
        first=self.service._ledger.snapshot()[0]["estimate"]
        self.message({"type":"response.output_text.done","response_id":"r","item_id":"item","text":"hello"})
        self.assertGreater(first,1)
        self.assertEqual(self.service._ledger.snapshot()[0]["estimate"],1)

    def test_interleaved_responses_route_by_item_id(self):
        for identity in ("a","b"):
            self.message({"type":"response.created","response":{"id":identity}})
            self.message({"type":"response.output_item.added","output_index":0,"item":self.item(identity=identity)})
        self.message({"type":"response.output_text.done","output_index":0,"item_id":"b","text":"hello"})
        self.message({"type":"response.output_text.done","output_index":0,"item_id":"a","text":"world"})
        self.assertEqual(self.tokens(),2)
        records={record["id"]:record["estimate"] for record in self.service._ledger.snapshot()}
        self.assertEqual(records,{"a":1,"b":1})

    def test_sse_crlf_split_at_every_byte(self):
        for identity in ("a","b"):
            raw=('data: '+json.dumps({"type":"response.output_text.done","item_id":identity,"text":"hello"})+'\r\n\r\n').encode()
            for byte in raw:
                self.service._handle("data","sse:bytes",bytes([byte]),"",self.now)
        self.assertEqual(self.tokens(),2)
        self.assertFalse(self.service._incomplete)


class StateCompatibilityTests(unittest.TestCase):
    def test_close_hides_root_with_withdrawn_child(self):
        root = tk.Tk()
        root.withdraw()
        child = tk.Toplevel(root)
        child.withdraw()
        window = ProxyWindow.__new__(ProxyWindow)
        window.root = root
        window._add_tray_icon = Mock()
        try:
            root.update()
            window._on_close()
            window._add_tray_icon.assert_called_once()
            self.assertEqual(root.state(), "withdrawn")
            root.deiconify()
            root.update()
            self.assertEqual(root.state(), "normal")
            window._on_close()
            root.update()
            self.assertEqual(root.state(), "withdrawn")
            self.assertEqual(window._add_tray_icon.call_count, 2)
        finally:
            root.destroy()

    def test_close_keeps_root_when_dialog_is_open(self):
        root = tk.Tk()
        root.withdraw()
        child = tk.Toplevel(root)
        window = ProxyWindow.__new__(ProxyWindow)
        window.root = root
        window._hide_to_tray = Mock()
        try:
            root.update()
            self.assertEqual(child.state(), "normal")
            window._on_close()
            window._hide_to_tray.assert_not_called()
        finally:
            root.destroy()

    def test_enum_values_keep_json_and_control_protocol_compatible(self):
        payload = json.dumps({"credential_type": CredentialType.RELAY_API, "load_strategy": LoadStrategy.PRIORITY})
        self.assertEqual(json.loads(payload), {"credential_type": "relay_api", "load_strategy": "priority"})
        self.assertEqual(f"{ProxyControlMessage.TOKEN_SPEED} {{}}\n".encode(), b"TOKEN_SPEED {}\n")
        self.assertEqual({ResetCreditResult.RESET: True}.get("reset"), True)

    def test_config_roundtrip_and_invalid_credential_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            service = AppConfigService(Path(directory) / "config.json")
            service.save(AppConfig(active_credential_type=CredentialType.RELAY_API))
            self.assertEqual(service.load().active_credential_type, CredentialType.RELAY_API)
            for invalid in ("unknown", [], {}):
                service.config_path.write_text(json.dumps({"active_credential_type": invalid}), encoding="utf-8")
                self.assertEqual(service.load().active_credential_type, CredentialType.CODEX_AUTH)

    def test_history_incomplete_state_does_not_depend_on_message(self):
        with tempfile.TemporaryDirectory() as directory:
            service = TokenHistoryService(Path(directory) / "history.sqlite3")
            try:
                service._set_error("普通提示中包含不完整")
                self.assertEqual(service._statistics_incomplete, "")
                service._set_error("记录写入异常", incomplete=True)
                self.assertEqual(service._statistics_incomplete, "记录写入异常")
            finally:
                service.stop()


class AutoResetCompensationTests(unittest.TestCase):
    def setUp(self):
        self.row = AuthFileRow("account", "refresh", "", file_name="auth.json", access_token="test-token")
        sync = Mock()
        sync.list_auth_rows.return_value = [self.row]
        self.service = AuthResetCreditService(sync)
        self.service.fetcher = Mock()
        self.info = ResetCreditInfo(2, credits=(ResetCredit("first", 0, 1180), ResetCredit("second", 0, 1180)))
        self.service.fetcher.fetch.return_value = self.info
        self.service.set_auto_use_enabled(True)
        self.service.fetcher.consume.side_effect = TimeoutError
        self.wall = patch("app.services.auth_reset_credit_service.time.time", return_value=1000)
        self.clock = patch("app.services.auth_reset_credit_service.time.monotonic", return_value=500)
        self.delay = patch("app.services.auth_reset_credit_service.random.uniform", return_value=20)
        self.wall.start()
        self.clock_mock = self.clock.start()
        self.delay.start()
        self.addCleanup(self.wall.stop)
        self.addCleanup(self.clock.stop)
        self.addCleanup(self.delay.stop)

    def test_timeout_retry_keeps_card_and_request_id(self):
        self.service.refresh_once()
        self.service._retry_due_once()
        self.assertEqual(self.service.fetcher.consume.call_count, 1)
        self.clock_mock.return_value = 521
        self.service._retry_due_once()
        calls = self.service.fetcher.consume.call_args_list
        self.assertEqual([call.args[3] for call in calls], ["first", "first"])
        self.assertEqual(calls[0].args[4], calls[1].args[4])
        self.assertFalse(self.service._auto_attempted)

    def test_query_failure_retains_pending_and_delays_retry(self):
        self.service.refresh_once()
        self.clock_mock.return_value = 521
        self.service.fetcher.fetch.side_effect = TimeoutError
        self.service._retry_due_once()
        self.service._retry_due_once()
        self.assertEqual(self.service.fetcher.consume.call_count, 1)
        self.assertEqual(self.service._auto_pending["account"].retry_at, 541)
        self.service.fetcher.fetch.side_effect = None
        self.clock_mock.return_value = 542
        self.service._retry_due_once()
        self.assertEqual(self.service.fetcher.consume.call_count, 2)

    def test_nothing_to_reset_and_unknown_results_keep_pending(self):
        for code in (ResetCreditResult.NOTHING_TO_RESET, "unknown"):
            self.service._auto_pending.clear()
            self.service.fetcher.consume.side_effect = None
            self.service.fetcher.consume.return_value = code
            self.service.refresh_once()
            self.assertIn("account", self.service._auto_pending)
            self.assertNotIn(("account", "first"), self.service._auto_attempted)

    def test_explicit_terminal_results_stop_retry(self):
        self.service.fetcher.consume.side_effect = None
        for code in (ResetCreditResult.RESET, ResetCreditResult.ALREADY_REDEEMED, ResetCreditResult.NO_CREDIT):
            self.service._auto_attempted.clear()
            self.service.fetcher.consume.return_value = code
            self.service.refresh_once()
            self.assertFalse(self.service._auto_pending)
            self.assertIn(("account", "first"), self.service._auto_attempted)
            self.assertIn(("account", "second"), self.service._auto_attempted)

    def test_server_removal_stops_pending_without_using_next_card(self):
        self.service.refresh_once()
        self.service.fetcher.fetch.return_value = ResetCreditInfo(1, credits=(self.info.credits[1],))
        self.clock_mock.return_value = 521
        self.service._retry_due_once()
        self.assertFalse(self.service._auto_pending)
        self.assertEqual(self.service.fetcher.consume.call_count, 1)

    def test_local_expiry_does_not_stop_server_available_card(self):
        self.service.refresh_once()
        with patch("app.services.auth_reset_credit_service.time.time", return_value=1200):
            self.clock_mock.return_value = 521
            self.service._retry_due_once()
        self.assertEqual(self.service.fetcher.consume.call_count, 2)

    def test_disable_and_shutdown_pause_compensation(self):
        self.service.refresh_once()
        self.clock_mock.return_value = 521
        self.service.set_auto_use_enabled(False)
        self.service._retry_due_once()
        self.service.set_auto_use_enabled(True)
        self.service.stop()
        self.service._retry_due_once()
        self.assertEqual(self.service.fetcher.consume.call_count, 1)

    def test_same_account_duplicate_rows_do_not_duplicate_consume(self):
        self.service.auth_sync_service.list_auth_rows.return_value = [self.row] * 4
        self.service.refresh_once()
        self.clock_mock.return_value = 521
        self.service._retry_due_once()
        self.assertEqual(self.service.fetcher.consume.call_count, 2)

    def test_credit_and_quota_countdowns_ignore_timezone_offsets(self):
        from datetime import datetime, timezone
        from app.utils.chatgpt_usage_fetcher import ChatGPTUsageFetcher
        stamps = ("2026-10-05T12:00:00Z", "2026-10-05T20:00:00+08:00", "2026-10-05T05:00:00-07:00")
        expiry = datetime.fromisoformat(stamps[0].replace("Z", "+00:00")).timestamp()
        window = ProxyWindow.__new__(ProxyWindow)
        for stamp in stamps:
            info = ChatGPTResetCreditFetcher.parse({"available_count": 1, "credits": [{"id": "card", "status": "available", "granted_at": "2026-10-01T00:00:00Z", "expires_at": stamp}]})
            self.assertEqual(info.expiry_timestamps, (expiry,))
            with patch("app.ui.proxy_window.time.time", return_value=expiry - 180):
                self.assertEqual(window._format_quota_refresh_countdown(stamp), "00:00:03:00")
        fetcher = ChatGPTUsageFetcher()
        text = fetcher._format_epoch_seconds(expiry)
        with patch("app.ui.proxy_window.time.time", return_value=expiry - 180):
            self.assertEqual(window._format_quota_refresh_countdown(text), "00:00:03:00")


if __name__ == "__main__":
    unittest.main()
