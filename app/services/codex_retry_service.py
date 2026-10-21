from __future__ import annotations

import json
import os
import threading
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

import psutil
import websocket

from app.services.codex_retry_script import RETRY_SCRIPT


class CodexRetryService:
    """通过 Chromium DevTools 监控 Codex 页面中的容量重试按钮。

    通过侧栏错误标记打开后台会话，检查重试入口后返回原会话。
    侧栏未展开或未加载的会话不在巡检范围内。
    """

    def __init__(self, interval_seconds: float = 5.0, *, port: int | None = None, enabled: bool = False) -> None:
        if port is None:
            try:
                port = int(os.environ.get("CODEX_DEBUG_PORT", "9222") or 0)
            except ValueError:
                port = 0
        if not 0 <= port <= 65535:
            raise ValueError("调试端口必须在 0 到 65535 之间（0 表示关闭调试连接）")
        self.port = port
        self.interval_seconds = interval_seconds
        self._enabled = enabled
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self._last_error_at = -float("inf")
        self._retry_cooldowns: dict[str, int] = {}
        self._initialized_targets: set[str] = set()

    @property
    def launch_arguments(self) -> list[str]:
        if not self.debugging_enabled:
            return []
        return [f"--remote-debugging-port={self.port}", "--remote-debugging-address=127.0.0.1"]

    @property
    def debugging_enabled(self) -> bool:
        return self.port > 0

    def running_without_debugging(self, exe: Path) -> bool:
        """Electron 会复用旧实例；旧实例没有对应参数时必须重新启动。"""
        for process in psutil.process_iter(["name", "exe", "cmdline"]):
            try:
                info = process.info
                if (info.get("name") or "").lower() != exe.name.lower():
                    continue
                if info.get("exe") and Path(info["exe"]).resolve() != exe.resolve():
                    continue
                args = info.get("cmdline") or []
                if any(arg.startswith("--type=") for arg in args):
                    continue
                has_port = any(
                    arg == f"--remote-debugging-port={self.port}" or
                    (arg == "--remote-debugging-port" and index + 1 < len(args)
                     and args[index + 1] == str(self.port))
                    for index, arg in enumerate(args)
                )
                if not has_port:
                    return True
            except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
                continue
        return False

    @property
    def enabled(self) -> bool:
        return self._enabled and self.debugging_enabled and os.environ.get("CODEX_AUTO_RETRY", "1") != "0"

    def set_enabled(self, enabled: bool) -> None:
        if self._enabled == enabled:
            return
        self._enabled = enabled
        self._initialized_targets.clear()
        if self.enabled:
            self.start()
        # 关闭时保留休眠线程，避免等待网络超时而阻塞 UI；它不再扫描或操作页面。
        print(f"[CodexRetry] 自动重试已{'开启' if self.enabled else '关闭'}", flush=True)

    def start(self) -> None:
        if not self.enabled or (self._thread is not None and self._thread.is_alive()):
            return
        self._stop.clear()
        print(f"[CodexRetry] 监控已启动: port={self.port} interval={self.interval_seconds}秒 cooldown=30秒 version=4", flush=True)
        self._thread = threading.Thread(target=self._run, name="codex-retry-monitor", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=4)
            if not self._thread.is_alive():
                self._thread = None

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                if self.enabled:
                    self._scan_once()
            except Exception as exc:
                if self.enabled:
                    self._report_error(exc)
            self._stop.wait(self.interval_seconds)

    def _report_error(self, exc: Exception) -> None:
        now = time.monotonic()
        if now - self._last_error_at >= 60:
            self._last_error_at = now
            print(f"[CodexRetry] 扫描失败（同类日志每分钟最多一次）: {exc}", flush=True)

    def _scan_once(self) -> None:
        if not self.enabled or self._stop.is_set():
            return
        with self._opener.open(f"http://127.0.0.1:{self.port}/json/list", timeout=1.5) as response:
            tabs = json.loads(response.read().decode("utf-8"))
        for tab in tabs:
            socket_url = tab.get("webSocketDebuggerUrl")
            url = tab.get("url", "")
            if not socket_url or not self._is_codex_page(tab):
                continue
            if self._stop.is_set() or not self.enabled:
                break
            try:
                self._scan_tab(socket_url, url)
            except Exception as exc:
                if self.enabled:
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
        if not self.enabled or self._stop.is_set():
            return
        ws = websocket.create_connection(socket_url, timeout=1.5, suppress_origin=True, http_no_proxy=["127.0.0.1", "localhost"])
        try:
            # 跨窗口和页面重载共用点击记录，冷却只从实际点击时开始计算。
            now = int(time.time() * 1000)
            self._retry_cooldowns = {
                key: timestamp for key, timestamp in self._retry_cooldowns.items()
                if now - timestamp <= 3600000
            }
            expression = ("window.__codexSessionManagerRetryCooldowns = " +
                          json.dumps(self._retry_cooldowns) + ";\n" + RETRY_SCRIPT)
            if socket_url not in self._initialized_targets:
                # 重新开启后取消上次巡检的返回动作，保留实际点击冷却记录。
                expression = ("if (window.__codexSessionManagerRetryState) "
                              "window.__codexSessionManagerRetryState.pending = null;\n" + expression)
            result = self._evaluate(ws, expression)
            self._initialized_targets.add(socket_url)
            outcome = result.get("result", {}).get("result", {}).get("value", {})
            if isinstance(outcome, dict) and outcome.get("action") == "retried":
                self._retry_cooldowns[str(outcome.get("cooldownKey") or url)] = int(outcome.get("clickedAt") or now)
            if isinstance(outcome, dict) and outcome.get("action") in ("retried", "opened", "returned", "cancelled"):
                print(f"[CodexRetry] {json.dumps(outcome, ensure_ascii=False)}", flush=True)
        finally:
            ws.close()

    def _evaluate(self, ws, expression: str) -> dict:
        if not self.enabled or self._stop.is_set():
            raise RuntimeError("重试监控已停止")
        ws.send(json.dumps({
            "id": 1,
            "method": "Runtime.evaluate",
            "params": {"expression": expression, "returnByValue": True},
        }))
        deadline = time.monotonic() + 1.5
        while not self._stop.is_set() and self.enabled:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("页面脚本执行超时")
            ws.settimeout(remaining)
            response = json.loads(ws.recv())
            if response.get("id") == 1:
                if response.get("error") or response.get("result", {}).get("exceptionDetails"):
                    raise RuntimeError(f"页面脚本执行失败: {response.get('error') or response['result']['exceptionDetails']}")
                return response
        raise RuntimeError("重试监控已停止")
