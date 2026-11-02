from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import socket
import shutil
import subprocess
import sys
from threading import Event, Lock, Thread
import webbrowser
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Callable
from urllib import error, parse, request

from app.services.auth_sync_service import AuthSyncService
from app.models import LoginCredentials
from app.services.browser_auth_service import BrowserAuthService


_AUTH_URL = "https://auth.openai.com/oauth/authorize"
_TOKEN_URL = "https://auth.openai.com/oauth/token"
_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
_CALLBACK_PATH = "/auth/callback"
_CALLBACK_PORTS = (1455, 1457)
_SCOPE = "openid profile email offline_access api.connectors.read api.connectors.invoke"
_OPENAI_AUTH_CLAIM = "https://api.openai.com/auth"


class _CallbackServer(HTTPServer):
    allow_reuse_address = False

    def server_bind(self) -> None:
        if sys.platform == "win32":
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


@dataclass
class AuthLoginResult:
    account_id: str
    refresh_token: str


@dataclass
class _PendingLogin:
    state: str
    code_verifier: str
    redirect_uri: str
    proxy_url: str
    expected_account_id: str = ""


class AuthLoginService:
    def __init__(self, auth_sync_service: AuthSyncService) -> None:
        self.auth_sync_service = auth_sync_service
        self._lock = Lock()
        self._callback_server: HTTPServer | None = None
        self._callback_port = 0
        self._callback_thread: Thread | None = None
        self._callback_port_hint: int | None = None
        self._pending_login: _PendingLogin | None = None
        self._result_callback: Callable[[AuthLoginResult | None, str], None] | None = None
        self._done = Event()
        self._active = False
        self._outcome: tuple[AuthLoginResult | None, str] | None = None

    def set_result_callback(self, callback: Callable[[AuthLoginResult | None, str], None]) -> None:
        self._result_callback = callback

    def start_callback_listener(
        self,
        log: Callable[[str], None] | None = None,
        callback_port: int | None = None,
    ) -> None:
        with self._lock:
            if self._callback_server is not None:
                return
            self._callback_port_hint = callback_port
            server = self._build_callback_server()
            self._callback_server = server
            self._callback_port = int(server.server_address[1])
            self._callback_thread = Thread(target=server.serve_forever, daemon=True)
            self._callback_thread.start()
        if log is not None:
            log(f"[AuthLogin] 登录回调监听已启动: http://localhost:{self._callback_port}{_CALLBACK_PATH}")

    def start_login(
        self,
        proxy_url: str = "",
        log: Callable[[str], None] | None = None,
        credentials: LoginCredentials | None = None,
        expected_account_id: str = "",
        callback_port: int | None = None,
    ) -> None:
        if credentials is not None:
            credentials.validate()
        with self._lock:
            if self._active:
                raise RuntimeError("已有登录流程正在进行，请完成或取消后重试。")
            self._active = True
            self._done = Event()
            self._outcome = None
        try:
            self.start_callback_listener(log, callback_port)
        except Exception:
            with self._lock:
                self._active = False
            raise
        code_verifier = self._token_urlsafe(96)
        code_challenge = self._code_challenge(code_verifier)
        state = self._token_urlsafe(32)
        redirect_uri = f"http://localhost:{self._callback_port}{_CALLBACK_PATH}"
        authorize_url = self._build_authorize_url(redirect_uri, code_challenge, state)

        with self._lock:
            self._pending_login = _PendingLogin(
                state=state,
                code_verifier=code_verifier,
                redirect_uri=redirect_uri,
                proxy_url=proxy_url,
                expected_account_id=expected_account_id,
            )
        Thread(
            target=self._login_worker, args=(authorize_url, credentials, proxy_url, log),
            daemon=True, name="browser-auth-login",
        ).start()

    def _login_worker(self, authorize_url, credentials, proxy_url, log) -> None:
        status = lambda message: log(f"[AuthLogin] {message}") if log is not None else None
        try:
            if credentials is not None and credentials.has_password:
                BrowserAuthService().run(authorize_url, credentials, self._done, status, proxy_url)
            else:
                status("凭据未配置完整，请在无痕窗口中手动登录")
                self._open_authorize_url(authorize_url)
                if not self._done.wait(300):
                    raise RuntimeError("登录超时，请重新更新授权。")
        except RuntimeError as exc:
            self._notify_result(None, str(exc))
        except Exception:
            # 浏览器异常可能携带输入内容，不将原始异常写入日志。
            self._notify_result(None, "自动登录未能完成，请重试或手动登录。")
        finally:
            self.stop_callback_listener()
            with self._lock:
                self._active = False
                outcome = self._outcome
            callback = self._result_callback
            if callback is not None and outcome is not None:
                callback(*outcome)

    def cancel_login(self) -> None:
        self._notify_result(None, "登录已取消。")

    def stop_callback_listener(self) -> None:
        with self._lock:
            server = self._callback_server
            self._callback_server = None
            thread = self._callback_thread
            self._callback_thread = None
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None:
            thread.join(timeout=2)

    def _open_authorize_url(self, authorize_url: str) -> None:
        """优先使用 Chrome 无痕窗口，找不到 Chrome 时回退到系统默认浏览器。"""
        if sys.platform == "win32":
            local_app_data = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
            candidates = [
                os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "Google", "Chrome", "Application", "chrome.exe"),
                os.path.join(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"), "Google", "Chrome", "Application", "chrome.exe"),
                os.path.join(local_app_data, "Google", "Chrome", "Application", "chrome.exe"),
                shutil.which("chrome.exe") or "",
            ]
            chrome_path = next((path for path in candidates if path and os.path.exists(path)), "")
            if chrome_path:
                try:
                    subprocess.Popen(
                        [chrome_path, "--incognito", "--new-window", authorize_url],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    return
                except OSError:
                    pass
        webbrowser.open(authorize_url)

    def _build_callback_server(self) -> HTTPServer:
        service = self

        class CallbackHandler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                parsed = parse.urlparse(self.path)
                if parsed.path != _CALLBACK_PATH:
                    self.send_response(404)
                    self.end_headers()
                    return

                params = parse.parse_qs(parsed.query)
                returned_state = params.get("state", [""])[0]
                pending_login = service._take_pending_login(returned_state)
                if pending_login is None:
                    self._send_page(400, "没有待处理登录，请返回账户管理工具重新点击更新授权。")
                    return

                error_text = params.get("error", [""])[0]
                if error_text:
                    service._notify_result(None, error_text)
                    self._send_page(400, "登录失败，请关闭此页面后重试。")
                    return

                code = params.get("code", [""])[0]
                if not code:
                    service._notify_result(None, "网页登录回调缺少 code。")
                    self._send_page(400, "登录失败，请关闭此页面后重试。")
                    return

                error_message = service._save_callback_login(code, pending_login)
                if error_message:
                    self._send_page(500, "登录失败，请返回账户管理工具查看错误。")
                    return
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

        ports = (self._callback_port_hint,) if self._callback_port_hint is not None else _CALLBACK_PORTS
        for port in ports:
            try:
                server = _CallbackServer(("127.0.0.1", port), CallbackHandler)
            except OSError:
                continue
            return server
        if self._callback_port_hint == 0:
            raise RuntimeError("无法监听随机登录回调端口。")
        raise RuntimeError("无法监听 Codex 登录回调端口 1455 或 1457。")

    def _take_pending_login(self, state: str) -> _PendingLogin | None:
        with self._lock:
            pending_login = self._pending_login
            if pending_login is None:
                return None
            if pending_login.state != state:
                return None
            self._pending_login = None
            return pending_login

    def _save_callback_login(self, code: str, pending_login: _PendingLogin) -> str:
        try:
            token_data = self._request_tokens(
                code,
                pending_login.code_verifier,
                pending_login.redirect_uri,
                pending_login.proxy_url,
            )
            auth_data = self._build_auth_data(token_data)
            account_id = str(auth_data["tokens"].get("account_id") or "")
            if pending_login.expected_account_id and account_id != pending_login.expected_account_id:
                raise RuntimeError("登录结果与选中账号不一致，未保存授权。")
            with self._lock:
                if self._done.is_set():
                    return "登录已取消，未保存授权。"
                account_id, refresh_token = self.auth_sync_service.save_logged_in_auth_file(auth_data)
        except Exception as exc:
            error_message = str(exc)
            self._notify_result(None, error_message)
            return error_message
        self._notify_result(AuthLoginResult(account_id=account_id, refresh_token=refresh_token), "")
        return ""

    def _notify_result(self, result: AuthLoginResult | None, error: str) -> None:
        with self._lock:
            if not self._active or self._done.is_set():
                return
            self._pending_login = None
            self._outcome = (result, error)
            self._done.set()

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
