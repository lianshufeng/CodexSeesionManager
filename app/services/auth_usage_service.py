from __future__ import annotations

import os
import random
import time
import traceback
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from queue import Empty, Queue
from threading import Event, Lock, Thread
from typing import Callable

from app.services.auth_sync_service import AuthSyncService
from app.utils.chatgpt_usage_fetcher import ChatGPTUsageFetcher


QUOTA_REFRESH_FAILED = "额度更新失败"


@dataclass(frozen=True, slots=True)
class AuthQuotaItem:
    refresh_token: str
    account_id: str
    quota: str
    plan_type: str = ""
    user_id: str = ""
    email: str = ""
    quota_refreshed_at_5h: str = ""
    quota_refreshed_at_7d: str = ""


class AuthUsageService:
    def __init__(
        self,
        auth_sync_service: AuthSyncService,
        fetcher: ChatGPTUsageFetcher | None = None,
        initial_delay_seconds: float = 3.0,
        interval_seconds: float = 30.0,
    ) -> None:
        self.auth_sync_service = auth_sync_service
        self.fetcher = fetcher or ChatGPTUsageFetcher()
        self.initial_delay_seconds = initial_delay_seconds
        self.interval_seconds = interval_seconds
        self._stop_event = Event()
        self._refresh_event = Event()
        self._thread: Thread | None = None
        self._on_change: Callable[[], None] | None = None
        self._on_quota_change: Callable[[], None] | None = None
        self._proxy_provider: Callable[[], str] | None = None
        self._quota_lock = Lock()
        self._quota_items_by_token: dict[str, AuthQuotaItem] = {}
        self._pending_items: Queue[AuthQuotaItem] = Queue()

    def set_change_callback(self, callback: Callable[[], None] | None) -> None:
        self._on_change = callback

    def set_quota_change_callback(self, callback: Callable[[], None] | None) -> None:
        self._on_quota_change = callback

    def set_proxy_provider(self, provider: Callable[[], str] | None) -> None:
        self._proxy_provider = provider

    def _log(self, message: str) -> None:
        print(f"[AuthUsage] {message}", flush=True)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._refresh_event.clear()
        self._log(f"后台线程启动: 首次 {self.initial_delay_seconds}s 后刷新")
        self._thread = Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._refresh_event.set()

    def request_refresh(self) -> None:
        self._refresh_event.set()

    def refresh_once(self) -> list[AuthQuotaItem]:
        return self._refresh_once()

    def quota_for(self, refresh_token: str) -> str:
        with self._quota_lock:
            item = self._quota_items_by_token.get(refresh_token)
            return item.quota if item is not None else ""

    def plan_type_for(self, refresh_token: str) -> str:
        with self._quota_lock:
            item = self._quota_items_by_token.get(refresh_token)
            return item.plan_type if item is not None else ""

    def user_id_for(self, refresh_token: str) -> str:
        with self._quota_lock:
            item = self._quota_items_by_token.get(refresh_token)
            return item.user_id if item is not None else ""

    def email_for(self, refresh_token: str) -> str:
        with self._quota_lock:
            item = self._quota_items_by_token.get(refresh_token)
            return item.email if item is not None else ""

    def update_quota_cache(self, cache: dict[str, str]) -> None:
        with self._quota_lock:
            self._quota_items_by_token = {
                refresh_token: AuthQuotaItem(refresh_token=refresh_token, account_id="", quota=quota)
                for refresh_token, quota in cache.items()
            }

    def remove_tokens(self, refresh_tokens: set[str]) -> None:
        if not refresh_tokens:
            return
        with self._quota_lock:
            for refresh_token in refresh_tokens:
                self._quota_items_by_token.pop(refresh_token, None)

    def pop_pending_quota_items(self) -> list[AuthQuotaItem]:
        latest: list[AuthQuotaItem] = []
        while True:
            try:
                latest.append(self._pending_items.get_nowait())
            except Empty:
                break
        return latest

    def quota_snapshot(self) -> dict[str, str]:
        with self._quota_lock:
            return {refresh_token: item.quota for refresh_token, item in self._quota_items_by_token.items()}

    def plan_type_snapshot(self) -> dict[str, str]:
        with self._quota_lock:
            return {refresh_token: item.plan_type for refresh_token, item in self._quota_items_by_token.items()}

    def user_id_snapshot(self) -> dict[str, str]:
        with self._quota_lock:
            return {refresh_token: item.user_id for refresh_token, item in self._quota_items_by_token.items()}

    def email_snapshot(self) -> dict[str, str]:
        with self._quota_lock:
            return {refresh_token: item.email for refresh_token, item in self._quota_items_by_token.items()}

    def quota_refresh_time_5h_snapshot(self) -> dict[str, str]:
        with self._quota_lock:
            return {
                refresh_token: item.quota_refreshed_at_5h
                for refresh_token, item in self._quota_items_by_token.items()
            }

    def quota_refresh_time_7d_snapshot(self) -> dict[str, str]:
        with self._quota_lock:
            return {
                refresh_token: item.quota_refreshed_at_7d
                for refresh_token, item in self._quota_items_by_token.items()
            }

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

    def _next_refresh_interval(self) -> float:
        return random.uniform(25.0, 35.0)

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

    def _refresh_once(self) -> list[AuthQuotaItem]:
        try:
            rows = self.auth_sync_service.list_auth_rows()
        except Exception as exc:
            self._log(f"读取授权文件失败: {exc}")
            return []
        if not rows:
            return []

        with self._quota_lock:
            cache = {refresh_token: item.quota for refresh_token, item in self._quota_items_by_token.items()}
            plan_type_cache = {refresh_token: item.plan_type for refresh_token, item in self._quota_items_by_token.items()}
            user_id_cache = {refresh_token: item.user_id for refresh_token, item in self._quota_items_by_token.items()}
            email_cache = {refresh_token: item.email for refresh_token, item in self._quota_items_by_token.items()}
            quota_refresh_time_cache = {
                refresh_token: item.quota_refreshed_at_5h for refresh_token, item in self._quota_items_by_token.items()
            }
            quota_refresh_time_7d_cache = {
                refresh_token: item.quota_refreshed_at_7d for refresh_token, item in self._quota_items_by_token.items()
            }

        changed = False
        proxy_url = self._proxy_provider() if self._proxy_provider is not None else ""
        with self._temporary_proxy_env(proxy_url):
            refresh_rows: list[tuple[str, str, str]] = []
            for row in rows:
                if not row.access_token:
                    continue
                refresh_rows.append((row.refresh_token, row.account_id, row.access_token))
            if not refresh_rows:
                return []
            with ThreadPoolExecutor(max_workers=len(refresh_rows), thread_name_prefix="auth-quota") as executor:
                futures = [
                    executor.submit(
                        self._refresh_row,
                        refresh_token,
                        account_id,
                        access_token,
                        cache,
                        plan_type_cache,
                        user_id_cache,
                        email_cache,
                        quota_refresh_time_cache,
                        quota_refresh_time_7d_cache,
                    )
                    for refresh_token, account_id, access_token in refresh_rows
                ]
        refreshed_items: list[AuthQuotaItem] = []
        for future in futures:
            try:
                item = future.result()
                if item is not None:
                    refreshed_items.append(item)
                    changed = True
            except Exception as exc:
                self._log(f"额度刷新线程异常: {exc}\n{traceback.format_exc()}")

        if changed:
            if self._on_quota_change is not None:
                self._on_quota_change()
            if self._on_change is not None:
                self._on_change()
        return refreshed_items

    def _refresh_row(
        self,
        refresh_token: str,
        account_id: str,
        access_token: str,
        cache: dict[str, str],
        plan_type_cache: dict[str, str],
        user_id_cache: dict[str, str],
        email_cache: dict[str, str],
        quota_refresh_time_cache: dict[str, str],
        quota_refresh_time_7d_cache: dict[str, str],
    ) -> AuthQuotaItem | None:
        try:
            result = self.fetcher.fetch(access_token, account_id)
        except Exception as exc:
            self._log(f"额度刷新失败: refresh_token={refresh_token} message={exc}")
            result = None
        if result is None:
            with self._quota_lock:
                item = AuthQuotaItem(
                    refresh_token=refresh_token,
                    account_id=account_id,
                    quota=QUOTA_REFRESH_FAILED,
                    plan_type=plan_type_cache.get(refresh_token, ""),
                    user_id=user_id_cache.get(refresh_token, ""),
                    email=email_cache.get(refresh_token, ""),
                    quota_refreshed_at_5h=quota_refresh_time_cache.get(refresh_token, ""),
                    quota_refreshed_at_7d=quota_refresh_time_7d_cache.get(refresh_token, ""),
                )
                self._quota_items_by_token[refresh_token] = item
                cache[refresh_token] = item.quota
            self._pending_items.put(item)
            return item
        if not result.quota:
            if result.message:
                self._log(f"额度刷新失败: refresh_token={refresh_token} message={result.message}")
            with self._quota_lock:
                item = AuthQuotaItem(
                    refresh_token=refresh_token,
                    account_id=account_id,
                    quota=QUOTA_REFRESH_FAILED,
                    plan_type=plan_type_cache.get(refresh_token, ""),
                    user_id=user_id_cache.get(refresh_token, ""),
                    email=email_cache.get(refresh_token, ""),
                    quota_refreshed_at_5h=quota_refresh_time_cache.get(refresh_token, ""),
                    quota_refreshed_at_7d=quota_refresh_time_7d_cache.get(refresh_token, ""),
                )
                self._quota_items_by_token[refresh_token] = item
                cache[refresh_token] = item.quota
            self._pending_items.put(item)
            return item

        plan_type = result.plan_type or plan_type_cache.get(refresh_token, "")
        user_id = result.user_id or user_id_cache.get(refresh_token, "")
        email = result.email or email_cache.get(refresh_token, "")
        quota_refreshed_at_5h = result.quota_refresh_time_5h or ""
        quota_refreshed_at_7d = result.quota_refresh_time_7d or ""

        with self._quota_lock:
            cache[refresh_token] = result.quota
            plan_type_cache[refresh_token] = plan_type
            user_id_cache[refresh_token] = user_id
            email_cache[refresh_token] = email
            quota_refresh_time_cache[refresh_token] = quota_refreshed_at_5h
            quota_refresh_time_7d_cache[refresh_token] = quota_refreshed_at_7d
            item = AuthQuotaItem(
                refresh_token=refresh_token,
                account_id=account_id,
                quota=result.quota,
                plan_type=plan_type,
                user_id=user_id,
                email=email,
                quota_refreshed_at_5h=quota_refreshed_at_5h,
                quota_refreshed_at_7d=quota_refreshed_at_7d,
            )
            self._quota_items_by_token[refresh_token] = item
        self._pending_items.put(item)
        return item
