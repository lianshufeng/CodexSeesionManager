import copy
import json
import tempfile
import unittest
import inspect
import re
import tkinter as tk
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from mitmproxy import http, websocket
from mitmproxy.test import tflow

from app.services.app_config_service import AppConfig, AppConfigService
from app.services.timezone_proxy_addon import TimezoneProxyAddon
from app.services.timezone_proxy_service import TimezoneProxyService
from app.services.timezone_rewrite_service import TimezoneRewriter, validate_timezone
from app.services.language_rewrite_service import LanguageRewriter, validate_language


def context(zone="Asia/Shanghai", day="2026-10-21", tag="environment_context"):
    return f"<{tag}>\n<current_date>{day}</current_date>\n<timezone>{zone}</timezone>\n</{tag}>"


def request(text=None, role="user"):
    return {"model": "unchanged", "input": [{"type": "message", "role": role,
            "content": [{"type": "input_text", "text": text or context()}]}]}


def text(payload):
    return payload["input"][0]["content"][0]["text"]


class TimezoneTests(unittest.TestCase):
    def test_cross_day_and_history(self):
        payload = request(context(day="2026-10-20"))
        now = datetime(2026, 10, 21, 15, 30, tzinfo=timezone.utc)
        self.assertTrue(TimezoneRewriter("Asia/Tokyo").rewrite(payload, now))
        self.assertIn("<timezone>Asia/Tokyo</timezone>", text(payload))
        self.assertIn("<current_date>2026-10-21</current_date>", text(payload))
        self.assertEqual(payload["model"], "unchanged")

    def test_tokyo_current_date_across_midnight(self):
        payload = request()
        TimezoneRewriter("Asia/Tokyo").rewrite(
            payload, datetime(2026, 10, 21, 15, 30, tzinfo=timezone.utc))
        self.assertIn("<current_date>2026-10-22</current_date>", text(payload))

    def test_desktop_developer_context(self):
        payload = request(context(tag="codex_apps_client_time_context"), "developer")
        self.assertTrue(TimezoneRewriter("Asia/Tokyo").rewrite(payload))
        self.assertIn("Asia/Tokyo", text(payload))

    def test_plain_user_text_tools_and_utc_are_untouched(self):
        for value in ("请解释 " + context(), "```xml\n" + context() + "\n```",
                      "<current_time_reminder>It is 2026-10-21 00:00:00 UTC.</current_time_reminder>"):
            payload = request(value)
            original = copy.deepcopy(payload)
            self.assertFalse(TimezoneRewriter("Asia/Tokyo").rewrite(payload))
            self.assertEqual(payload, original)
        payload = request(role="assistant")
        self.assertFalse(TimezoneRewriter("Asia/Tokyo").rewrite(payload))

    def test_incremental_date_update(self):
        rewriter = TimezoneRewriter("Asia/Tokyo")
        now = datetime(2026, 10, 21, 15, 30, tzinfo=timezone.utc)
        rewriter.rewrite(request(), now)
        payload = request("<environment_context><current_date>2026-10-21</current_date></environment_context>")
        self.assertTrue(rewriter.rewrite(payload, now))
        self.assertIn("2026-10-22", text(payload))

    def test_invalid_zone(self):
        for value in ("", "Japan/Fake", "../Asia/Tokyo"):
            with self.assertRaises((ValueError, KeyError)):
                validate_timezone(value)

    def test_config_round_trip_and_legacy_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            service = AppConfigService(Path(directory) / "config.json")
            service.config_path.write_text('{"port":8080}', encoding="utf-8")
            self.assertEqual(service.load().timezone_name, "Asia/Tokyo")
            self.assertTrue(service.load().timezone_override)
            service.save(AppConfig(timezone_override=False, timezone_name="Europe/London"))
            self.assertFalse(service.load().timezone_override)
            self.assertEqual(service.load().timezone_name, "Europe/London")

    def test_packaged_proxy_environment_can_include_local_relays(self):
        from app.services.codex_local_config_service import CodexLocalConfigService
        with tempfile.TemporaryDirectory() as directory:
            service = CodexLocalConfigService(Path(directory))
            service.env_path.write_text('CUSTOM="keep"\n', encoding="utf-8")
            self.assertTrue(service.set_proxy_environment("http://127.0.0.1:18081", bypass_local=False)[0])
            self.assertIn('NO_PROXY=""', service.env_path.read_text(encoding="utf-8"))
            self.assertTrue(service.set_proxy_environment(None)[0])
            self.assertEqual(service.env_path.read_text(encoding="utf-8"), 'CUSTOM="keep"\n')

    def test_compressed_http_and_non_target(self):
        for encoding in ("identity", "gzip", "zstd"):
            addon = TimezoneProxyAddon("Asia/Tokyo")
            flow = tflow.tflow()
            flow.request = http.Request.make("POST", "https://example.test/backend-api/codex/responses",
                                             json.dumps(request()).encode(), {"Content-Type": "application/json"})
            if encoding != "identity":
                flow.request.encode(encoding)
            addon.request(flow)
            self.assertIn("Asia/Tokyo", text(json.loads(flow.request.get_text())))
            self.assertEqual(int(flow.request.headers["content-length"]), len(flow.request.raw_content))
            original = flow.request.raw_content
            flow.request.path = "/other"
            addon.request(flow)
            self.assertEqual(flow.request.raw_content, original)

    def test_bad_json_keeps_original(self):
        addon = TimezoneProxyAddon("Asia/Tokyo")
        flow = tflow.tflow()
        flow.request = http.Request.make("POST", "https://example.test/responses", b"invalid")
        addon.request(flow)
        self.assertEqual(flow.request.raw_content, b"invalid")

    def test_websocket_client_only(self):
        addon = TimezoneProxyAddon("Asia/Tokyo")
        flow = tflow.tflow()
        flow.request.path = "/backend-api/codex/responses"
        flow.websocket = websocket.WebSocketData()
        payload = request()
        payload["type"] = "response.create"
        message = websocket.WebSocketMessage(1, True, json.dumps(payload).encode())
        flow.websocket.messages.append(message)
        addon.websocket_message(flow)
        self.assertIn("Asia/Tokyo", text(json.loads(message.content)))
        server_message = websocket.WebSocketMessage(1, False, json.dumps(payload).encode())
        flow.websocket.messages.append(server_message)
        original = server_message.content
        addon.websocket_message(flow)
        self.assertEqual(server_message.content, original)

    def test_instance_proxy_has_no_global_stop_fallback(self):
        with patch.object(TimezoneProxyService, "find_available_port", return_value=18081):
            proxy = TimezoneProxyService("Asia/Tokyo", 18080)
        self.assertEqual(proxy.config.host, "127.0.0.1")
        self.assertEqual(proxy.config.upstream_proxy, "127.0.0.1:18080")
        with patch("app.services.proxy_service.ProxyService._stop_matching_processes") as global_stop:
            proxy.stop()
            global_stop.assert_not_called()

    def test_proxy_environment_is_private(self):
        with patch.object(TimezoneProxyService, "find_available_port", return_value=18081), \
                patch.dict("os.environ", {}, clear=False):
            import os
            original = os.environ.get("CODEX_SESSION_TIMEZONE")
            proxy = TimezoneProxyService("Asia/Tokyo", 18080)
            self.assertEqual(proxy._process_environment()["CODEX_SESSION_TIMEZONE"], "Asia/Tokyo")
            self.assertEqual(os.environ.get("CODEX_SESSION_TIMEZONE"), original)


class LaunchTests(unittest.TestCase):
    def test_real_ui_controls_without_starting_services(self):
        from app.ui.proxy_window import ProxyWindow
        root = tk.Tk()
        root.withdraw()
        self.addCleanup(root.destroy)
        window = ProxyWindow.__new__(ProxyWindow)
        window.root = root
        for name in set(re.findall(r"self\.(\w*var)\b", inspect.getsource(ProxyWindow._build_ui))):
            setattr(window, name, tk.StringVar(root, value="0"))
        window.timezone_name_var.set("Asia/Tokyo")
        window.timezone_override_var.set("1")
        window.language_name_var.set("ja-JP")
        window.language_override_var.set("1")
        window._timezone_launch_busy = False
        window._bind_widget_tooltip = Mock()
        window._schedule_refresh_launch_buttons = Mock()
        window._persist_config = Mock()
        window._build_ui()
        root.update_idletasks()
        self.assertEqual(window._timezone_combo.get(), "Asia/Tokyo")
        self.assertEqual(window._language_combo.get(), "ja-JP")
        self.assertIn("en-US", window._language_combo["values"])
        self.assertIn("America/New_York", window._timezone_combo["values"])
        self.assertTrue(window._timezone_combo.bind("<<ComboboxSelected>>"))
        window.timezone_override_var.set("")
        window._on_timezone_edited()
        self.assertEqual(str(window._timezone_combo["state"]), "disabled")
        self.assertEqual(str(window._language_combo["state"]), "normal")
        window._persist_config.assert_called_once()

    def test_relay_launch_routes_through_instance_proxy(self):
        from app.ui.proxy_window import ProxyWindow
        with tempfile.TemporaryDirectory() as directory:
            exe = Path(directory) / "ChatGPT.exe"
            exe.touch()
            window = ProxyWindow.__new__(ProxyWindow)
            window.service = Mock()
            window.service.process.poll.return_value = None
            window.service.ensure_launch_permissions.return_value = (True, "")
            window._is_relay_active = Mock(return_value=True)
            from app.services.codex_retry_service import CodexRetryService
            window.codex_retry_service = CodexRetryService(port=9222)
            with patch("app.ui.proxy_window.subprocess.Popen") as spawn, \
                    patch.object(window.codex_retry_service, "start"), \
                    patch.object(window.codex_retry_service, "running_without_debugging", return_value=False):
                self.assertTrue(window._launch_codex_process(SimpleNamespace(path=str(exe)), 18081, True))
                args = spawn.call_args.args[0]
                self.assertIn("--proxy-server=http://127.0.0.1:18081", args)
                self.assertNotIn("--no-proxy-server", args)
                self.assertIn("--remote-debugging-port=9222", args)
                self.assertIn("--remote-debugging-address=127.0.0.1", args)
                self.assertEqual(spawn.call_args.kwargs["env"]["HTTPS_PROXY"], "http://127.0.0.1:18081")
                self.assertEqual(spawn.call_args.kwargs["env"]["NO_PROXY"], "")

    def test_launch_and_failure_recover_controls(self):
        from app.ui.proxy_window import ProxyWindow
        window = ProxyWindow.__new__(ProxyWindow)
        window._timezone_launch_busy = True
        window._closing = False
        window._set_timezone_launch_busy = Mock()
        window._service_status_var = Mock()
        window.service = Mock()
        window.service.process.poll.return_value = None
        window._timezone_proxies = []
        proxy = Mock(timezone_name="Asia/Tokyo")
        window._launch_codex_process = Mock(return_value=True)
        window._finish_timezone_launch("row", proxy, "")
        self.assertFalse(window._timezone_launch_busy)
        window._launch_codex_process.assert_called_once_with("row", proxy.config.port, True)
        self.assertEqual(window._timezone_proxies, [proxy])
        window._timezone_proxies = []
        window._launch_codex_process.return_value = False
        window._finish_timezone_launch("row", proxy, "")
        proxy.stop.assert_called_once()
        self.assertEqual(window._timezone_proxies, [])


class LanguageTests(unittest.TestCase):
    def test_config_defaults_and_roundtrip(self):
        with tempfile.TemporaryDirectory() as directory:
            config = AppConfigService(Path(directory) / "config.json")
            config.config_path.write_text('{"timezone_name":"Asia/Tokyo"}', encoding="utf-8")
            self.assertTrue(config.load().language_override)
            self.assertEqual(config.load().language_name, "ja-JP")
            config.save(AppConfig(language_override=False, language_name="en-US"))
            self.assertFalse(config.load().language_override)
            self.assertEqual(config.load().language_name, "en-US")

    def test_language_validation(self):
        self.assertEqual(validate_language(" JA_jp "), "ja-JP")
        self.assertEqual(validate_language("zh-hant-tw"), "zh-Hant-TW")
        for value in ("", "日语", "ja-JP\r\nX-Test: yes", "ja;q=0.5"):
            with self.assertRaises(ValueError):
                validate_language(value)

    def test_existing_metadata_and_context(self):
        payload = request("<environment_context><os_language>zh-CN</os_language>"
                          "<locale>zh-CN</locale><timezone>Asia/Shanghai</timezone></environment_context>")
        payload["client_metadata"] = {"locale": "zh-CN", "country": "CN", "other": "zh-CN"}
        payload["_meta"] = {"openai/locale": "zh-CN", "openai/userLocation": {"country": "CN"}}
        rewriter = LanguageRewriter("ja-JP")
        self.assertTrue(rewriter.rewrite(payload))
        self.assertIn("<os_language>ja-JP</os_language>", text(payload))
        self.assertIn("<locale>ja-JP</locale>", text(payload))
        self.assertIn("Asia/Shanghai", text(payload))
        self.assertEqual(payload["client_metadata"], {"locale": "ja-JP", "country": "CN", "other": "zh-CN"})
        self.assertEqual(payload["_meta"]["openai/locale"], "ja-JP")
        self.assertEqual(payload["_meta"]["openai/userLocation"], {"country": "CN"})
        self.assertFalse(rewriter.rewrite(payload))

    def test_no_fields_invented_and_user_text_untouched(self):
        payload = request("系统语言是 zh-CN，请解释 locale 字段")
        payload["input"].append({"type": "function_call", "arguments": '{"language":"zh-CN"}'})
        original = copy.deepcopy(payload)
        self.assertFalse(LanguageRewriter("ja-JP").rewrite(payload))
        self.assertEqual(payload, original)

    def test_mcp_metadata_only(self):
        payload = {"params": {"_meta": {"openai/locale": "en-US"}, "arguments": {"language": "en-US"}}}
        self.assertTrue(LanguageRewriter("ja-JP").rewrite(payload))
        self.assertEqual(payload["params"]["_meta"]["openai/locale"], "ja-JP")
        self.assertEqual(payload["params"]["arguments"]["language"], "en-US")

    def test_language_only_proxy_has_private_environment(self):
        import os
        with patch.object(TimezoneProxyService, "find_available_port", return_value=18081), \
                patch.dict(os.environ, {"CODEX_SESSION_TIMEZONE": "Europe/London"}):
            proxy = TimezoneProxyService(None, 18080, "ja-JP")
            env = proxy._process_environment()
            self.assertNotIn("CODEX_SESSION_TIMEZONE", env)
            self.assertEqual(env["CODEX_SESSION_LANGUAGE"], "ja-JP")
            self.assertEqual(os.environ["CODEX_SESSION_TIMEZONE"], "Europe/London")

    def test_headers_metadata_compression_and_disabled(self):
        for compression in (None, "gzip", "zstd"):
            addon = TimezoneProxyAddon(language_name="ja-JP")
            flow = tflow.tflow()
            payload = request()
            payload["client_metadata"] = {"locale": "zh-CN"}
            flow.request = http.Request.make("POST", "https://example.test/responses",
                json.dumps(payload).encode(), {"Content-Type": "application/json", "Accept-Language": "zh-CN"})
            if compression:
                flow.request.encode(compression)
            addon.request(flow)
            updated = json.loads(flow.request.get_text())
            self.assertEqual(flow.request.headers["accept-language"], "ja-JP,ja;q=0.9")
            self.assertEqual(updated["client_metadata"]["locale"], "ja-JP")
            self.assertIn("Asia/Shanghai", text(updated))
            self.assertEqual(int(flow.request.headers["content-length"]), len(flow.request.raw_content))
        addon = TimezoneProxyAddon("Asia/Tokyo")
        flow = tflow.tflow()
        flow.request.headers["Accept-Language"] = "zh-CN"
        addon.request(flow)
        self.assertEqual(flow.request.headers["Accept-Language"], "zh-CN")

    def test_websocket_language_only_and_server_untouched(self):
        addon = TimezoneProxyAddon(language_name="ja-JP")
        flow = tflow.tflow()
        flow.request.path = "/responses"
        flow.websocket = websocket.WebSocketData()
        payload = request()
        payload.update(type="response.create", client_metadata={"locale": "zh-CN"})
        for from_client in (True, False):
            msg = websocket.WebSocketMessage(1, from_client, json.dumps(payload).encode())
            flow.websocket.messages.append(msg)
            addon.websocket_message(flow)
            parsed = json.loads(msg.content)
            self.assertEqual(parsed["client_metadata"]["locale"], "ja-JP" if from_client else "zh-CN")
            self.assertIn("Asia/Shanghai", text(parsed))

    def test_language_only_launch_ignores_disabled_timezone(self):
        from app.ui.proxy_window import ProxyWindow
        with tempfile.TemporaryDirectory() as directory:
            exe = Path(directory) / "ChatGPT.exe"
            exe.touch()
            window = ProxyWindow.__new__(ProxyWindow)
            window._timezone_launch_busy = False
            window._closing = False
            window.timezone_override_var = Mock(get=Mock(return_value=False))
            window.language_override_var = Mock(get=Mock(return_value=True))
            window.timezone_name_var = Mock(get=Mock(return_value="invalid zone"))
            window.language_name_var = Mock(get=Mock(return_value="ja-JP"))
            window._timezone_proxies = []
            window._stop_timezone_proxies = Mock()
            window._persist_config = Mock()
            window._set_timezone_launch_busy = Mock()
            window._service_status_var = Mock()
            window._post_ui = Mock()
            window.service = Mock()
            window.service.process.poll.return_value = None
            window.service.config.port = 18080
            with patch("app.ui.proxy_window.psutil.process_iter", return_value=[]), \
                    patch("app.ui.proxy_window.TimezoneProxyService") as factory, \
                    patch("app.ui.proxy_window.Thread") as thread:
                factory.return_value.run.return_value = (True, "")
                window._launch_codex(SimpleNamespace(path=str(exe)))
                thread.call_args.kwargs["target"]()
                factory.assert_called_once_with(None, 18080, "ja-JP")


if __name__ == "__main__":
    unittest.main()
