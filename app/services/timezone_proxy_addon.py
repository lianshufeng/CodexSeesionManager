from __future__ import annotations

import json
import os

from mitmproxy import http

try:
    from app.services.timezone_rewrite_service import TimezoneRewriter
except ModuleNotFoundError as exc:
    if exc.name != "app":
        raise
    from timezone_rewrite_service import TimezoneRewriter


class TimezoneProxyAddon:
    def __init__(self, timezone_name: str | None = None) -> None:
        self.rewriter = TimezoneRewriter(timezone_name or os.environ["CODEX_SESSION_TIMEZONE"])
        self._reported = False

    def load(self, loader) -> None:
        print(f"[Timezone] 时区规则已准备: {self.rewriter.timezone_name}，等待实际请求", flush=True)

    @staticmethod
    def _is_responses(flow: http.HTTPFlow) -> bool:
        path = flow.request.path.split("?", 1)[0].rstrip("/")
        return path.endswith("/responses") or path.endswith("/responses/compact")

    def _rewrite(self, text: str) -> str | None:
        payload = json.loads(text)
        if not self.rewriter.rewrite(payload):
            return None
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    def _report(self) -> None:
        if not self._reported:
            self._reported = True
            print(f"[Timezone] 实际请求上下文已改写: {self.rewriter.timezone_name}", flush=True)

    def request(self, flow: http.HTTPFlow) -> None:
        if flow.request.method != "POST" or not self._is_responses(flow):
            return
        try:
            # get_text/set_text decode and re-encode Content-Encoding, including zstd.
            rewritten = self._rewrite(flow.request.get_text(strict=True) or "")
            if rewritten is not None:
                flow.request.set_text(rewritten)
                self._report()
        except (ValueError, UnicodeError) as exc:
            print(f"[Timezone] 无法改写请求，已保留原文: {type(exc).__name__}", flush=True)

    def websocket_message(self, flow: http.HTTPFlow) -> None:
        if not self._is_responses(flow) or not flow.websocket or not flow.websocket.messages:
            return
        message = flow.websocket.messages[-1]
        if not message.from_client:
            return
        try:
            text = message.content.decode("utf-8")
            payload = json.loads(text)
            if not isinstance(payload, dict) or payload.get("type") != "response.create":
                return
            if self.rewriter.rewrite(payload):
                message.content = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                self._report()
        except (ValueError, UnicodeError) as exc:
            print(f"[Timezone] 无法改写 WebSocket，已保留原文: {type(exc).__name__}", flush=True)


addons = [TimezoneProxyAddon()] if os.environ.get("CODEX_SESSION_TIMEZONE") else []
