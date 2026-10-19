from __future__ import annotations

import json
import time
from threading import Event
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener


class WarmupRejected(RuntimeError):
    """服务端明确拒绝请求，可以在退避后重试。"""


class CodexWarmupClient:
    base_url = "https://chatgpt.com/backend-api/codex"
    client_version = "0.163.0"

    def _open(self, token, account_id, proxy_url, path, payload=None):
        headers = {
            "Authorization": "Bearer " + token,
            "ChatGPT-Account-Id": account_id,
            "User-Agent": "codex-tui/" + self.client_version,
            "originator": "codex_cli_rs",
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
            exc.close()
            if code < 500:
                raise WarmupRejected(f"请求被拒绝（HTTP {code}），请检查模型和账号权限") from None
            raise TimeoutError(f"服务端错误（HTTP {code}），结果待确认") from None

    def check_model(self, token: str, account_id: str, proxy_url: str, model: str) -> None:
        with self._open(token, account_id, proxy_url, "/models?client_version=" + self.client_version) as response:
            catalog = json.loads(response.read(2 * 1024 * 1024))
        models = catalog.get("models", [])
        if not any(item.get("slug") == model for item in models if isinstance(item, dict)):
            raise WarmupRejected("该账号不支持预热模型 " + model)

    def ping(self, token: str, account_id: str, proxy_url: str, model: str, stop: Event) -> None:
        payload = {
            "model": model, "instructions": "Reply with pong only.",
            "input": [{"role": "user", "content": [{"type": "input_text", "text": "ping"}]}],
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
