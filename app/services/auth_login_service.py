from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
import webbrowser
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Callable
from urllib import error, parse, request

from app.services.auth_sync_service import AuthSyncService


_AUTH_URL = "https://auth.openai.com/oauth/authorize"
_TOKEN_URL = "https://auth.openai.com/oauth/token"
_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
_CALLBACK_PATH = "/auth/callback"
_CALLBACK_PORTS = (1455, 1457)
_SCOPE = "openid profile email offline_access api.connectors.read api.connectors.invoke"
_OPENAI_AUTH_CLAIM = "https://api.openai.com/auth"


@dataclass
class AuthLoginResult:
    account_id: str
    refresh_token: str


class AuthLoginService:
    def __init__(self, auth_sync_service: AuthSyncService) -> None:
        self.auth_sync_service = auth_sync_service

    def login_and_save(
        self,
        proxy_url: str = "",
        timeout_seconds: int = 300,
        log: Callable[[str], None] | None = None,
    ) -> AuthLoginResult:
        code_verifier = self._token_urlsafe(96)
        code_challenge = self._code_challenge(code_verifier)
        state = self._token_urlsafe(32)
        server = self._build_callback_server(state)
        port = int(server.server_address[1])
        redirect_uri = f"http://localhost:{port}{_CALLBACK_PATH}"
        authorize_url = self._build_authorize_url(redirect_uri, code_challenge, state)

        if log is not None:
            log(f"[AuthLogin] 等待网页登录回调: {redirect_uri}")
        webbrowser.open(authorize_url)

        try:
            code = self._wait_for_code(server, timeout_seconds)
        finally:
            server.server_close()

        token_data = self._request_tokens(code, code_verifier, redirect_uri, proxy_url)
        auth_data = self._build_auth_data(token_data)
        account_id, refresh_token = self.auth_sync_service.save_logged_in_auth_file(auth_data)
        return AuthLoginResult(account_id=account_id, refresh_token=refresh_token)

    def _build_callback_server(self, state: str) -> HTTPServer:
        callback_state = {"code": "", "error": ""}

        class CallbackHandler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                parsed = parse.urlparse(self.path)
                if parsed.path != _CALLBACK_PATH:
                    self.send_response(404)
                    self.end_headers()
                    return

                params = parse.parse_qs(parsed.query)
                returned_state = params.get("state", [""])[0]
                if returned_state != state:
                    callback_state["error"] = "网页登录状态校验失败。"
                    self._send_page(400, "登录失败，请关闭此页面后重试。")
                    return

                error_text = params.get("error", [""])[0]
                if error_text:
                    callback_state["error"] = error_text
                    self._send_page(400, "登录失败，请关闭此页面后重试。")
                    return

                code = params.get("code", [""])[0]
                if not code:
                    callback_state["error"] = "网页登录回调缺少 code。"
                    self._send_page(400, "登录失败，请关闭此页面后重试。")
                    return

                callback_state["code"] = code
                self._send_page(200, "登录成功，可以返回账户管理工具。")

            def log_message(self, _format: str, *_args: object) -> None:
                return

            def _send_page(self, status: int, message: str) -> None:
                body = (
                    "<!doctype html><html><head><meta charset=\"utf-8\">"
                    "<title>Codex 登录</title></head><body>"
                    f"<p>{message}</p>"
                    "</body></html>"
                ).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        for port in _CALLBACK_PORTS:
            try:
                server = HTTPServer(("127.0.0.1", port), CallbackHandler)
            except OSError:
                continue
            server.callback_state = callback_state  # type: ignore[attr-defined]
            return server
        raise RuntimeError("无法监听 Codex 登录回调端口 1455 或 1457。")

    def _wait_for_code(self, server: HTTPServer, timeout_seconds: int) -> str:
        server.timeout = 1
        deadline = time.monotonic() + timeout_seconds
        callback_state = server.callback_state  # type: ignore[attr-defined]
        while time.monotonic() < deadline:
            server.handle_request()
            code = str(callback_state.get("code") or "")
            if code:
                return code
            error_text = str(callback_state.get("error") or "")
            if error_text:
                raise RuntimeError(error_text)
        raise RuntimeError("等待网页登录回调超时。")

    def _request_tokens(
        self,
        code: str,
        code_verifier: str,
        redirect_uri: str,
        proxy_url: str,
    ) -> dict[str, object]:
        payload = parse.urlencode(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "client_id": _CLIENT_ID,
                "code_verifier": code_verifier,
            }
        ).encode("utf-8")
        req = request.Request(
            _TOKEN_URL,
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        opener = self._build_opener(proxy_url)
        try:
            with opener.open(req, timeout=30) as resp:
                body = resp.read().decode("utf-8", errors="replace")
        except error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"换取网页登录令牌失败: HTTP {exc.code}: {self._extract_error_message(body)}") from exc
        except error.URLError as exc:
            raise RuntimeError(f"换取网页登录令牌失败: {exc.reason}") from exc

        try:
            data = json.loads(body)
        except json.JSONDecodeError as exc:
            raise RuntimeError("网页登录令牌接口返回非 JSON。") from exc
        if not isinstance(data, dict):
            raise RuntimeError("网页登录令牌接口返回格式无效。")
        return data

    def _build_auth_data(self, token_data: dict[str, object]) -> dict[str, object]:
        id_token = str(token_data.get("id_token") or "").strip()
        access_token = str(token_data.get("access_token") or "").strip()
        refresh_token = str(token_data.get("refresh_token") or "").strip()
        if not id_token or not access_token or not refresh_token:
            raise RuntimeError("网页登录令牌接口返回缺少必要 token。")

        account_id = self._extract_account_id(id_token)
        return {
            "auth_mode": "chatgpt",
            "tokens": {
                "id_token": id_token,
                "access_token": access_token,
                "refresh_token": refresh_token,
                "account_id": account_id,
            },
            "last_refresh": datetime.now(timezone.utc).isoformat(),
        }

    def _extract_account_id(self, id_token: str) -> str:
        parts = id_token.split(".")
        if len(parts) < 2:
            return ""
        payload = parts[1]
        payload += "=" * (-len(payload) % 4)
        try:
            raw = base64.urlsafe_b64decode(payload.encode("ascii"))
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, json.JSONDecodeError):
            return ""
        if not isinstance(data, dict):
            return ""
        openai_auth = data.get(_OPENAI_AUTH_CLAIM)
        if isinstance(openai_auth, dict):
            account_id = str(openai_auth.get("chatgpt_account_id") or "")
            if account_id:
                return account_id
        return str(data.get("account_id") or data.get("chatgpt_account_id") or "")

    def _build_authorize_url(self, redirect_uri: str, code_challenge: str, state: str) -> str:
        params = {
            "response_type": "code",
            "client_id": _CLIENT_ID,
            "redirect_uri": redirect_uri,
            "scope": _SCOPE,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "id_token_add_organizations": "true",
            "codex_cli_simplified_flow": "true",
            "state": state,
            "originator": "Codex Session Manager",
            "codex_streamlined_login": "true",
        }
        return f"{_AUTH_URL}?{parse.urlencode(params)}"

    def _build_opener(self, proxy_url: str) -> request.OpenerDirector:
        proxy = proxy_url.strip()
        if not proxy:
            return request.build_opener()
        if "://" not in proxy:
            proxy = f"http://{proxy}"
        return request.build_opener(request.ProxyHandler({"http": proxy, "https": proxy}))

    def _code_challenge(self, code_verifier: str) -> str:
        digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
        return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")

    def _token_urlsafe(self, byte_count: int) -> str:
        return secrets.token_urlsafe(byte_count).rstrip("=")

    def _extract_error_message(self, body: str) -> str:
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            return body.strip() or "请求失败"
        if isinstance(data, dict):
            error_value = data.get("error")
            if isinstance(error_value, dict):
                return str(error_value.get("message") or error_value.get("code") or error_value)
            if isinstance(error_value, str):
                description = str(data.get("error_description") or "").strip()
                return f"{error_value}: {description}" if description else error_value
            return str(data.get("message") or data.get("code") or data)
        return str(data)
