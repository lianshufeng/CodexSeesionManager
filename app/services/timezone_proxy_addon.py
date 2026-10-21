from __future__ import annotations

import json
import os

from mitmproxy import http

try:
    from app.services.timezone_rewrite_service import TimezoneRewriter
    from app.services.language_rewrite_service import LanguageRewriter
except ModuleNotFoundError as exc:
    if exc.name != "app":
        raise
    from timezone_rewrite_service import TimezoneRewriter
    from language_rewrite_service import LanguageRewriter


class TimezoneProxyAddon:
    def __init__(self, timezone_name: str | None = None, language_name: str | None = None) -> None:
        timezone_name = timezone_name or os.environ.get("CODEX_SESSION_TIMEZONE")
        language_name = language_name or os.environ.get("CODEX_SESSION_LANGUAGE")
        self.rewriter = TimezoneRewriter(timezone_name) if timezone_name else None
        self.language_rewriter = LanguageRewriter(language_name) if language_name else None
        self._reported = False
        self._language_header_reported = False
        self._language_context_reported = False
        self._last_timezone_changed = False
        self._last_language_changed = False

    def load(self, loader) -> None:
        if self.rewriter:
            print(f"[Timezone] 时区规则已准备: {self.rewriter.timezone_name}，等待实际请求", flush=True)
        if self.language_rewriter:
            print(f"[Language] 语言规则已准备: {self.language_rewriter.language}，等待实际请求", flush=True)

    @staticmethod
    def _is_responses(flow: http.HTTPFlow) -> bool:
        path = flow.request.path.split("?", 1)[0].rstrip("/")
        return path.endswith("/responses") or path.endswith("/responses/compact")

    def _rewrite(self, text: str, *, include_timezone: bool = True) -> str | None:
        payload = json.loads(text)
        if not self._rewrite_payload(payload, include_timezone=include_timezone):
            return None
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    def _rewrite_payload(self, payload: object, *, include_timezone: bool = True) -> bool:
        timezone_changed = self.rewriter.rewrite(payload) if self.rewriter and include_timezone else False
        language_changed = self.language_rewriter.rewrite(payload) if self.language_rewriter else False
        self._last_timezone_changed = timezone_changed
        self._last_language_changed = language_changed
        return timezone_changed or language_changed

    def _report_payload(self) -> None:
        if self._last_timezone_changed:
            self._report()
        if self._last_language_changed and not self._language_context_reported:
            self._language_context_reported = True
            print(f"[Language] 实际请求语言字段已改写: {self.language_rewriter.language}", flush=True)

    def _report(self) -> None:
        if not self._reported:
            self._reported = True
            print(f"[Timezone] 实际请求上下文已改写: {self.rewriter.timezone_name}", flush=True)

    def request(self, flow: http.HTTPFlow) -> None:
        if flow.request.method == "CONNECT":
            return
        if self.language_rewriter:
            flow.request.headers["Accept-Language"] = self.language_rewriter.accept_language
            if not self._language_header_reported:
                self._language_header_reported = True
                print(f"[Language] 实际请求语言头已设置: {self.language_rewriter.accept_language}", flush=True)
        if not flow.request.raw_content or (not self._is_responses(flow) and not self.language_rewriter):
            return
        if not self._is_responses(flow) and "json" not in flow.request.headers.get("content-type", "").lower():
            return
        try:
            # get_text/set_text decode and re-encode Content-Encoding, including zstd.
            rewritten = self._rewrite(flow.request.get_text(strict=True) or "", include_timezone=self._is_responses(flow))
            if rewritten is not None:
                flow.request.set_text(rewritten)
                self._report_payload()
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
            if self._rewrite_payload(payload):
                message.content = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                self._report_payload()
        except (ValueError, UnicodeError) as exc:
            print(f"[Timezone] 无法改写 WebSocket，已保留原文: {type(exc).__name__}", flush=True)


addons = [TimezoneProxyAddon()] if (
    os.environ.get("CODEX_SESSION_TIMEZONE") or os.environ.get("CODEX_SESSION_LANGUAGE")
) else []
