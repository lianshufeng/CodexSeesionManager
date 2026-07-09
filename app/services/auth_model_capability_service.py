from __future__ import annotations

import json
import os
import random
import time
import traceback
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from threading import Event, Lock, Thread
from typing import Callable, Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from app.services.auth_sync_service import AuthSyncService


_CHINA_TIMEZONE = timezone(timedelta(hours=8))
_MODELS_ENDPOINT = "https://chatgpt.com/backend-api/codex/models"
_CLIENT_VERSION = "0.144.0"


@dataclass(frozen=True, slots=True)
class ModelCapability:
    slug: str
    use_responses_lite: bool
    prefer_websockets: bool


@dataclass(frozen=True, slots=True)
class AuthModelCapabilityItem:
    refresh_token: str
    account_id: str
    status: str
    message: str = ""
    fetched_at: str = ""
    models: tuple[ModelCapability, ...] = ()


class ChatGPTModelsFetcher:
    def fetch(self, access_token: str, account_id: str = "") -> tuple[tuple[ModelCapability, ...], str]:
        if not access_token:
            return (), "access_token 为空"

        query = urlencode({"client_version": _CLIENT_VERSION})
        headers = {
            "user-agent": f"codex-tui/{_CLIENT_VERSION} (Windows 10.0.22631; x86_64)",
            "authorization": f"Bearer {access_token}",
            "accept": "application/json",
            "host": "chatgpt.com",
        }
        if account_id:
            headers["chatgpt-account-id"] = account_id

        request = Request(f"{_MODELS_ENDPOINT}?{query}", headers=headers)
        try:
            with urlopen(request, timeout=20) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
            return (), str(exc)

        models = self._parse_models(payload)
        if not models:
            return (), "模型目录为空"
        return tuple(models), ""

    def _parse_models(self, payload: Any) -> list[ModelCapability]:
        if not isinstance(payload, dict):
            return []
        raw_models = payload.get("models")
        if not isinstance(raw_models, list):
            return []
        models: list[ModelCapability] = []
        for item in raw_models:
            if not isinstance(item, dict):
                continue
            slug = str(item.get("slug") or item.get("id") or "").strip()
            if not slug:
                continue
            models.append(
                ModelCapability(
                    slug=slug,
                    use_responses_lite=bool(item.get("use_responses_lite", False)),
                    prefer_websockets=bool(item.get("prefer_websockets", False)),
                )
            )
        return sorted(models, key=lambda item: item.slug)


class AuthModelCapabilityService:
    def __init__(
        self,
        auth_sync_service: AuthSyncService,
        fetcher: ChatGPTModelsFetcher | None = None,
        initial_delay_seconds: float = 12.0,
        interval_seconds: float = 300.0,
    ) -> None:
        self.auth_sync_service = auth_sync_service
        self.fetcher = fetcher or ChatGPTModelsFetcher()
        self.initial_delay_seconds = initial_delay_seconds
        self.interval_seconds = interval_seconds
        self._stop_event = Event()
        self._refresh_event = Event()
        self._thread: Thread | None = None
        self._lock = Lock()
        self._items_by_token: dict[str, AuthModelCapabilityItem] = {}
        self._on_change: Callable[[], None] | None = None
        self._proxy_provider: Callable[[], str] | None = None

    def set_change_callback(self, callback: Callable[[], None] | None) -> None:
        self._on_change = callback

    def set_proxy_provider(self, provider: Callable[[], str] | None) -> None:
        self._proxy_provider = provider

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._refresh_event.clear()
        self._thread = Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._refresh_event.set()

    def request_refresh(self) -> None:
        self._refresh_event.set()

    def item_for(self, refresh_token: str) -> AuthModelCapabilityItem | None:
        with self._lock:
            return self._items_by_token.get(refresh_token)

    def _log(self, message: str) -> None:
        print(f"[AuthModels] {message}", flush=True)

    def _run(self) -> None:
        try:
            if self._wait_for_refresh(self.initial_delay_seconds):
                return
            self._refresh_once()
            while not self._stop_event.is_set():
                if self._wait_for_refresh(self._next_refresh_interval()):
                    return
                self._refresh_once()
        except Exception as exc:
            self._log(f"后台线程异常: {exc}\n{traceback.format_exc()}")

    def _wait_for_refresh(self, seconds: float) -> bool:
        deadline = time.monotonic() + seconds
        while not self._stop_event.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            if self._refresh_event.wait(min(remaining, 0.5)):
                self._refresh_event.clear()
                return self._stop_event.is_set()
        return True

    def _next_refresh_interval(self) -> float:
        return self.interval_seconds + random.uniform(0.0, 60.0)

    def _refresh_once(self) -> None:
        rows = [row for row in self.auth_sync_service.list_auth_rows() if row.access_token]
        if not rows:
            return
        proxy_url = self._proxy_provider() if self._proxy_provider is not None else ""
        changed = False
        with self._temporary_proxy_env(proxy_url):
            for row in rows:
                if self._stop_event.is_set():
                    return
                if self._refresh_row(row.refresh_token, row.account_id, row.access_token):
                    changed = True
                self._stop_event.wait(random.uniform(1.5, 4.0))
        if changed and self._on_change is not None:
            self._on_change()

    def _refresh_row(self, refresh_token: str, account_id: str, access_token: str) -> bool:
        models, message = self.fetcher.fetch(access_token, account_id)
        status = "ok" if models else "failed"
        fetched_at = datetime.now(_CHINA_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")
        item = AuthModelCapabilityItem(
            refresh_token=refresh_token,
            account_id=account_id,
            status=status,
            message=message,
            fetched_at=fetched_at,
            models=models,
        )
        with self._lock:
            previous = self._items_by_token.get(refresh_token)
            self._items_by_token[refresh_token] = item
        if status == "ok":
            self._log(f"已刷新模型能力: account_id={account_id} models={len(models)}")
        else:
            self._log(f"模型能力刷新失败: account_id={account_id} message={message}")
        return previous != item

    def _normalize_proxy_url(self, proxy_url: str) -> str:
        value = proxy_url.strip()
        if not value:
            return ""
        if "://" not in value:
            value = f"http://{value}"
        return value

    @contextmanager
    def _temporary_proxy_env(self, proxy_url: str):
        normalized = self._normalize_proxy_url(proxy_url)
        if not normalized:
            yield
            return

        env_keys = ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")
        previous = {key: os.environ.get(key) for key in env_keys}
        for key in env_keys:
            os.environ[key] = normalized
        try:
            yield
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
