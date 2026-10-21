"""容量重试的服务测试，以及在临时无头浏览器中执行真实页面脚本的测试。"""

import html
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.request import ProxyHandler, build_opener

import websocket

from app.services.codex_retry_script import RETRY_SCRIPT
from app.services.codex_retry_service import CodexRetryService


CAPACITY = "Selected model is at capacity. Please try a different model."


class RetryServiceTests(unittest.TestCase):
    def test_port_configuration_and_disable(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(CodexRetryService().port, 9222)
            self.assertEqual(CodexRetryService(0.1).interval_seconds, 0.1)
            self.assertFalse(CodexRetryService(port=0).enabled)
            self.assertEqual(CodexRetryService(port=0).launch_arguments, [])
            for port in (-1, 65536):
                with self.assertRaises(ValueError):
                    CodexRetryService(port=port)
        with patch.dict(os.environ, CODEX_DEBUG_PORT="9233", CODEX_AUTO_RETRY="0"):
            self.assertEqual(CodexRetryService().port, 9233)
            self.assertEqual(CodexRetryService().launch_arguments, [])

    def test_old_desktop_instance_requires_restart_but_renderer_is_ignored(self):
        service = CodexRetryService(port=9222)
        exe = Path("ChatGPT.exe").resolve()
        process = Mock(info={"name": exe.name, "exe": str(exe), "cmdline": [str(exe)]})
        with patch("app.services.codex_retry_service.psutil.process_iter", return_value=[process]):
            self.assertTrue(service.running_without_debugging(exe))
            for args in ([str(exe), "--remote-debugging-port=9222"],
                         [str(exe), "--remote-debugging-port", "9222"],
                         [str(exe), "--type=renderer"]):
                process.info["cmdline"] = args
                self.assertFalse(service.running_without_debugging(exe))

    def test_disconnects_do_not_end_monitoring(self):
        service = CodexRetryService(interval_seconds=0.01, port=9222)
        service._scan_once = Mock(side_effect=[OSError("disconnected"), OSError("not ready"), None])

        def tick(_interval):
            if service._scan_once.call_count == 3:
                service._stop.set()

        with patch.object(service._stop, "wait", side_effect=tick), patch.object(service, "_report_error"):
            service._run()
        self.assertEqual(service._scan_once.call_count, 3)

    def test_non_codex_pages_are_excluded(self):
        for url in ("https://chatgpt.com/", "about:blank", "app://-/index.html?avatar-overlay"):
            self.assertFalse(CodexRetryService._is_codex_page({"type": "page", "url": url}))
        self.assertTrue(CodexRetryService._is_codex_page({"type": "page", "url": "app://-/index.html"}))


class RetryBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        candidates = [os.environ.get("CODEX_RETRY_TEST_BROWSER"), shutil.which("google-chrome"),
                      shutil.which("chromium"), r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                      r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"]
        browser = next((path for path in candidates if path and Path(path).is_file()), None)
        if not browser:
            raise unittest.SkipTest("浏览器脚本测试需要 Chrome 或 Edge")
        cls.profile = tempfile.TemporaryDirectory(prefix="codex-retry-test-", ignore_cleanup_errors=True)
        cls.addClassCleanup(cls.profile.cleanup)
        process = subprocess.Popen(
            [browser, "--headless=new", "--remote-debugging-port=0", "--remote-debugging-address=127.0.0.1",
             f"--user-data-dir={cls.profile.name}", "--no-first-run", "--no-default-browser-check",
             "--disable-background-networking", "--no-proxy-server", "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

        def stop_browser():
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)

        cls.addClassCleanup(stop_browser)
        port_file = Path(cls.profile.name) / "DevToolsActivePort"
        deadline = time.monotonic() + 15
        while not port_file.exists() and time.monotonic() < deadline and process.poll() is None:
            time.sleep(0.1)
        if not port_file.exists():
            raise RuntimeError("临时测试浏览器未开启调试端口")
        port = int(port_file.read_text().splitlines()[0])
        opener = build_opener(ProxyHandler({}))
        with opener.open(f"http://127.0.0.1:{port}/json/list", timeout=5) as response:
            cls.tab = next(tab for tab in json.load(response) if tab["type"] == "page")
        cls.ws = websocket.create_connection(cls.tab["webSocketDebuggerUrl"], timeout=5,
                                             suppress_origin=True, http_no_proxy=["127.0.0.1", "localhost"])
        cls.addClassCleanup(cls.ws.close)

    def setUp(self):
        self.service = CodexRetryService(port=9222)
        self.evaluate("window.__codexSessionManagerRetryState = undefined; "
                      "window.__codexSessionManagerRetryCooldowns = {}; window.clicks = 0; "
                      f"window.testNow = {int(time.time() * 1000)}; Date.now = () => window.testNow;")
        self.fixture()

    def evaluate(self, expression):
        response = self.service._evaluate(self.ws, expression)
        return response.get("result", {}).get("result", {}).get("value")

    def fixture(self, label="重试", message=CAPACITY, attributes="", tag="button"):
        # 故意先放一个无关的 Reset 按钮，验证不会沿祖先扩散匹配。
        markup = ('<button id="unrelated" onclick="window.wrongClick = true">Reset settings</button>'
                  '<nav><button data-app-action-sidebar-thread-row '
                  'data-app-action-sidebar-thread-id="one" '
                  'data-app-action-sidebar-thread-selected="true">one</button></nav>'
                  '<main><aside><span class="wrap-anywhere">' + html.escape(message) + '</span>'
                  f'<div><{tag} id="retry" {attributes} onclick="window.clicks++">'
                  + html.escape(label) + f'</{tag}></div></aside></main>')
        self.evaluate("document.body.innerHTML = " + json.dumps(markup) + "; window.wrongClick = false;")

    def scan(self):
        return self.evaluate(RETRY_SCRIPT)

    def advance(self, milliseconds=30000):
        self.evaluate(f"window.testNow += {milliseconds}")

    def test_chinese_english_and_compact_capacity(self):
        for label in ("重试", "重新尝试", "再试一次", "立即重置", "Retry", "RETRY NOW", "Try again", "Reset now"):
            with self.subTest(label=label):
                self.fixture(label, "Error running remote compact task: " + CAPACITY)
                self.assertEqual(self.scan()["action"], "retried")
                self.assertFalse(self.evaluate("window.wrongClick"))
                self.advance()
        self.fixture("重试", "所选模型容量已满，请尝试其他模型。")
        self.assertEqual(self.scan()["action"], "retried")

    def test_countdown_waits_even_if_button_is_enabled(self):
        for label in ("5 秒后重置", "重试（剩余 12 秒）", "Retry in 8s", "Reset in 3 seconds"):
            with self.subTest(label=label):
                self.fixture(label)
                self.assertNotEqual(self.scan()["action"], "retried")
                self.assertEqual(self.evaluate("window.clicks"), 0)
        self.evaluate("document.getElementById('retry').textContent = '0 秒后重试'")
        self.assertEqual(self.scan()["action"], "retried")

    def test_disabled_hidden_and_aria_labels(self):
        for attributes in ('disabled', 'aria-disabled="true"', 'hidden', 'style="visibility:hidden"'):
            with self.subTest(attributes=attributes):
                self.fixture(attributes=attributes)
                self.assertNotEqual(self.scan()["action"], "retried")
        self.fixture("", attributes='role="button" aria-label="Retry"', tag="div")
        self.assertEqual(self.scan()["action"], "retried")

    def test_non_capacity_error_and_transcript_text_do_not_match(self):
        for message in ("Error running remote compact task: Network disconnected", "Unable to load settings"):
            self.fixture(message=message)
            self.assertNotEqual(self.scan()["action"], "retried")
        self.fixture()
        self.evaluate("document.querySelector('aside').outerHTML = '<pre>' + "
                      "document.querySelector('aside').textContent + '</pre>'")
        self.assertNotEqual(self.scan()["action"], "retried")

    def test_unlimited_retries_with_exact_thirty_second_cooldown(self):
        for iteration in range(20):
            self.assertEqual(self.scan()["action"], "retried")
            # React 重新渲染按钮也不能绕过会话级冷却。
            self.fixture()
            self.advance(29999)
            self.assertNotEqual(self.scan()["action"], "retried")
            self.advance(1)
        self.assertEqual(self.evaluate("window.clicks"), 20)

    def test_cooldowns_are_independent_for_each_thread(self):
        self.assertEqual(self.scan()["action"], "retried")
        self.evaluate("document.querySelector('nav button').setAttribute('data-app-action-sidebar-thread-id', 'two')")
        self.assertEqual(self.scan()["action"], "retried")
        self.evaluate("document.querySelector('nav button').setAttribute('data-app-action-sidebar-thread-id', 'one')")
        self.assertNotEqual(self.scan()["action"], "retried")

    def test_service_uses_real_cdp_and_preserves_cooldown_after_page_state_is_lost(self):
        descriptor = dict(self.tab, url="app://-/index.html")
        response = Mock()
        response.read.return_value = json.dumps([descriptor]).encode()
        self.service._opener = Mock()
        self.service._opener.open.return_value.__enter__ = Mock(return_value=response)
        self.service._opener.open.return_value.__exit__ = Mock(return_value=False)
        self.service._scan_once()
        self.assertEqual(self.evaluate("window.clicks"), 1)
        self.evaluate("window.__codexSessionManagerRetryState = undefined")
        self.service._scan_once()
        self.assertEqual(self.evaluate("window.clicks"), 1)
        self.advance()
        self.service._scan_once()
        self.assertEqual(self.evaluate("window.clicks"), 2)

    def sidebar_fixture(self):
        self.fixture(message="healthy")
        markup = ('<button data-app-action-sidebar-thread-row data-app-action-sidebar-thread-id="two">'
                  'two<svg class="text-danger" aria-hidden="false"></svg></button>'
                  '<button data-app-action-sidebar-thread-row data-app-action-sidebar-thread-id="three">three</button>')
        self.evaluate("document.querySelector('nav').insertAdjacentHTML('beforeend', " + json.dumps(markup) + ");" +
                      "for (const row of document.querySelectorAll('nav button')) { row.onclick = () => {" +
                      "for (const item of document.querySelectorAll('nav button')) " +
                      "item.setAttribute('data-app-action-sidebar-thread-selected', item === row ? 'true' : 'false');" +
                      "document.querySelector('aside span').textContent = " +
                      "row.getAttribute('data-app-action-sidebar-thread-id') === 'two' ? " + json.dumps(CAPACITY) +
                      " : 'healthy'; }; }")

    def test_background_retry_returns_and_repeats_after_cooldown(self):
        self.sidebar_fixture()
        self.assertEqual(self.scan()["action"], "opened")
        self.advance(5000)
        self.assertEqual(self.scan()["action"], "retried")
        self.advance(5000)
        self.assertEqual(self.scan()["action"], "returned")
        self.assertEqual(self.evaluate("document.querySelector('[data-app-action-sidebar-thread-selected=true]')"
                                      ".getAttribute('data-app-action-sidebar-thread-id')"), "one")
        self.advance(20000)
        self.assertNotEqual(self.scan()["action"], "opened")
        self.advance(5000)
        self.assertEqual(self.scan()["action"], "opened")
        self.assertEqual(self.scan()["action"], "retried")

    def test_typing_and_manual_navigation_are_respected(self):
        self.sidebar_fixture()
        self.evaluate("document.body.insertAdjacentHTML('beforeend', '<textarea></textarea>');"
                      "document.querySelector('textarea').focus()")
        self.assertEqual(self.scan().get("reason"), "editing")
        self.evaluate("document.querySelector('textarea').blur()")
        self.assertEqual(self.scan()["action"], "opened")
        self.evaluate("document.querySelector('[data-app-action-sidebar-thread-id=three]').click()")
        self.assertEqual(self.scan(), {"action": "cancelled", "reason": "user_navigation"})


class RetryLaunchTests(unittest.TestCase):
    def test_packaged_app_receives_debug_arguments(self):
        from app.ui.proxy_window import ProxyWindow

        with tempfile.TemporaryDirectory() as directory:
            exe = Path(directory) / "OpenAI.Codex_1.0.0_x64__test" / "app" / "ChatGPT.exe"
            exe.parent.mkdir(parents=True)
            exe.touch()
            window = ProxyWindow.__new__(ProxyWindow)
            window.service = Mock()
            window.service.process.poll.return_value = None
            window.codex_local_config_service = Mock()
            window.codex_local_config_service.set_proxy_environment.return_value = (True, "")
            window._is_relay_active = Mock(return_value=True)
            window.codex_retry_service = CodexRetryService(port=9233)
            with patch("app.ui.proxy_window.activate_packaged_app") as activate, \
                    patch.object(window.codex_retry_service, "start") as start, \
                    patch.object(window.codex_retry_service, "running_without_debugging", return_value=False):
                self.assertTrue(window._launch_codex_process(SimpleNamespace(path=str(exe)), 18081))
                self.assertIn("--remote-debugging-port=9233", activate.call_args.args[1])
                self.assertIn("--remote-debugging-address=127.0.0.1", activate.call_args.args[1])
                start.assert_called_once()

    def test_existing_instance_without_debugging_is_not_silently_reused(self):
        from app.ui.proxy_window import ProxyWindow

        with tempfile.TemporaryDirectory() as directory:
            exe = Path(directory) / "ChatGPT.exe"
            exe.touch()
            window = ProxyWindow.__new__(ProxyWindow)
            window.service = Mock()
            window.service.process.poll.return_value = None
            window.service.ensure_launch_permissions.return_value = (True, "")
            window._is_relay_active = Mock(return_value=True)
            window.codex_retry_service = CodexRetryService(port=9222)
            with patch.object(window.codex_retry_service, "running_without_debugging", return_value=True), \
                    patch("app.ui.proxy_window.messagebox.showwarning") as warning, \
                    patch("app.ui.proxy_window.subprocess.Popen") as spawn:
                self.assertFalse(window._launch_codex_process(SimpleNamespace(path=str(exe)), 18081))
                warning.assert_called_once()
                spawn.assert_not_called()


if __name__ == "__main__":
    unittest.main()
