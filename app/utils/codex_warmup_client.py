from __future__ import annotations

import json
import platform
import random
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from threading import Event
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.version import APP_VERSION


class WarmupRejected(RuntimeError):
    """服务端明确拒绝请求，可以在退避后重试。"""

    def __init__(self, message: str, retry_after: float = 3600) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class CodexWarmupClient:
    base_url = "https://chatgpt.com/backend-api/codex"
    client_version = "0.163.0"

    def __init__(self) -> None:
        # 采用官方 UA 的字段结构，使用管理器身份和真实系统信息。
        self.originator = "codex_session_manager"
        value = (f"{self.originator}/{APP_VERSION} "
                 f"({platform.system()} {platform.version()}; {platform.machine()}) "
                 f"Python/{platform.python_version()}")
        self.user_agent = "".join(c if " " <= c <= "~" else "_" for c in value)

    @staticmethod
    def _input(timezone_name: str | None = None, now: datetime | None = None) -> list[dict]:
        now = now or datetime.now(timezone.utc)
        try:
            local = now.astimezone(ZoneInfo(timezone_name)) if timezone_name else now.astimezone()
        except (KeyError, ZoneInfoNotFoundError):
            local = now.astimezone()
        zone = timezone_name or getattr(local.tzinfo, "key", None) or str(local.tzinfo)
        left, right = random.randint(1, 50), random.randint(1, 50)
        text = random.choice((f"What is {left} + {right}?", f"What is {left} minus {right}?",
                              "Translate 'hello' into Japanese.", "How many days are in a week?"))
        return [
            {"role": "user", "content": [{"type": "input_text", "text": (
                f"<environment_context>\n<current_date>{local.date().isoformat()}</current_date>\n"
                f"<timezone>{zone}</timezone>\n</environment_context>")}]},
            {"role": "user", "content": [{"type": "input_text", "text": text}]},
        ]

    def _open(self, token, account_id, proxy_url, path, payload=None):
        headers = {
            "Authorization": "Bearer " + token,
            "ChatGPT-Account-Id": account_id,
            "User-Agent": self.user_agent,
            "originator": self.originator,
            "Accept": "text/event-stream" if payload is not None else "application/json",
        }
        body = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(payload).encode("utf-8")
        proxy_url = proxy_url.strip()
        if proxy_url and "://" not in proxy_url:
            proxy_url = "http://" + proxy_url
        # 显式指定代理，避免环境中的管理器代理改写账号令牌。
        opener = build_opener(ProxyHandler({"http": proxy_url, "https": proxy_url} if proxy_url else {}))
        try:
            return opener.open(Request(self.base_url + path, data=body, headers=headers), timeout=20)
        except HTTPError as exc:
            code = exc.code
            retry_after = 3600.0
            value = exc.headers.get("Retry-After") if exc.headers else None
            if value:
                try:
                    retry_after = max(retry_after, float(value))
                except ValueError:
                    try:
                        retry_after = max(retry_after, parsedate_to_datetime(value).timestamp() - time.time())
                    except (ValueError, TypeError, OverflowError):
                        pass
            exc.close()
            if code < 500:
                raise WarmupRejected(f"请求被拒绝（HTTP {code}），请检查模型和账号权限", retry_after) from None
            raise TimeoutError(f"服务端错误（HTTP {code}），结果待确认") from None

    def check_model(self, token: str, account_id: str, proxy_url: str, model: str) -> None:
        with self._open(token, account_id, proxy_url, "/models?client_version=" + self.client_version) as response:
            catalog = json.loads(response.read(2 * 1024 * 1024))
        models = catalog.get("models", [])
        if not any(item.get("slug") == model for item in models if isinstance(item, dict)):
            raise WarmupRejected("该账号不支持预热模型 " + model)

    def ping(self, token: str, account_id: str, proxy_url: str, model: str, stop: Event,
             *, timezone_name: str | None = None) -> None:
        payload = {
            "model": model, "instructions": "Answer briefly with only the result. Do not use tools.",
            "input": self._input(timezone_name),
            "tools": [], "tool_choice": "none", "parallel_tool_calls": False,
            "reasoning": {"effort": "low"}, "store": False, "stream": True,
            "include": [],
        }
        deadline = time.monotonic() + 60
        with self._open(token, account_id, proxy_url, "/responses", payload) as response:
            while not stop.is_set() and time.monotonic() < deadline:
                line = response.readline(128 * 1024)
                if not line:
                    break
                if not line.startswith(b"data:"):
                    continue
                data = line[5:].strip()
                if data == b"[DONE]":
                    break
                event = json.loads(data)
                if event.get("type") == "response.completed":
                    return
                if event.get("type") in {"error", "response.failed", "response.incomplete"}:
                    raise TimeoutError("预热请求未完成，结果待确认")
        raise TimeoutError("预热结果待确认")
