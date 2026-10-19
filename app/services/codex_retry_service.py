from __future__ import annotations

import json
import os
import threading
import time
import urllib.request
from urllib.parse import urlsplit

import websocket

from app.services.codex_retry_script import RETRY_SCRIPT


class CodexRetryService:
    """通过 Chromium DevTools 监控 Codex 页面中的容量重试按钮。

    通过侧栏错误标记打开后台会话，检查重试入口后返回原会话。
    侧栏未展开或未加载的会话不在巡检范围内。
    """

    def __init__(self, interval_seconds: float = 5.0) -> None:
        try:
            self.port = int(os.environ.get("CODEX_DEBUG_PORT", "0") or 0)
        except ValueError:
            self.port = 0
        self.interval_seconds = interval_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self._last_error_at = 0.0

    @property
    def enabled(self) -> bool:
        return self.port > 0 and os.environ.get("CODEX_AUTO_RETRY", "1") != "0"

    def start(self) -> None:
        if not self.enabled or self._thread is not None:
            return
        self._stop.clear()
        print(f"[CodexRetry] 监控已启动: port={self.port} interval={self.interval_seconds}秒 version=3", flush=True)
        self._thread = threading.Thread(target=self._run, name="codex-retry-monitor", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._scan_once()
            except Exception as exc:
                self._report_error(exc)
            self._stop.wait(self.interval_seconds)

    def _report_error(self, exc: Exception) -> None:
        now = time.monotonic()
        if now - self._last_error_at >= 60:
            self._last_error_at = now
            print(f"[CodexRetry] 扫描失败（同类日志每分钟最多一次）: {exc}", flush=True)

    def _scan_once(self) -> None:
        with self._opener.open(f"http://127.0.0.1:{self.port}/json/list", timeout=1.5) as response:
            tabs = json.loads(response.read().decode("utf-8"))
        for tab in tabs:
            socket_url = tab.get("webSocketDebuggerUrl")
            url = tab.get("url", "")
            if not socket_url or not self._is_codex_page(tab):
                continue
            if self._stop.is_set():
                break
            try:
                self._scan_tab(socket_url, url)
            except Exception as exc:
                self._report_error(exc)

    @staticmethod
    def _is_codex_page(tab: dict) -> bool:
        parsed = urlsplit(tab.get("url", ""))
        return (
            tab.get("type") == "page"
            and parsed.scheme == "app"
            and parsed.netloc == "-"
            and parsed.path in ("/index.html", "/detached-window.html")
            and "avatar-overlay" not in parsed.query
        )

    def _scan_tab(self, socket_url: str, url: str) -> None:
        ws = websocket.create_connection(socket_url, timeout=1.5, suppress_origin=True, http_no_proxy=["127.0.0.1", "localhost"])
        try:
            result = self._evaluate(ws, RETRY_SCRIPT)
            outcome = result.get("result", {}).get("result", {}).get("value", {})
            if isinstance(outcome, dict) and outcome.get("action") in ("retried", "opened", "returned", "cancelled"):
                print(f"[CodexRetry] {json.dumps(outcome, ensure_ascii=False)}", flush=True)
        finally:
            ws.close()

    def _evaluate(self, ws, expression: str) -> dict:
        ws.send(json.dumps({
            "id": 1,
            "method": "Runtime.evaluate",
            "params": {"expression": expression, "returnByValue": True},
        }))
        while True:
            response = json.loads(ws.recv())
            if response.get("id") == 1:
                if response.get("error") or response.get("result", {}).get("exceptionDetails"):
                    raise RuntimeError(f"页面脚本执行失败: {response.get('error') or response['result']['exceptionDetails']}")
                return response
