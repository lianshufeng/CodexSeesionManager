from __future__ import annotations

import random
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock, Thread
from typing import Callable

from app.models import (AutoResetCreditAttempt, ResetCredit, ResetCreditInfo,
                        ResetCreditResult, ResetCreditState)
from app.services.auth_sync_service import AuthFileRow, AuthSyncService
from app.utils.chatgpt_reset_credit_fetcher import ChatGPTResetCreditFetcher


_AUTO_USE_WINDOW_SECONDS = 180
_AUTO_RETRY_MIN_SECONDS = 10.0
_AUTO_RETRY_MAX_SECONDS = 30.0
_AUTO_USE_TERMINAL_RESULTS = frozenset((ResetCreditResult.RESET,
                                      ResetCreditResult.ALREADY_REDEEMED,
                                      ResetCreditResult.NO_CREDIT))


class AuthResetCreditService:
    def __init__(self, auth_sync_service: AuthSyncService) -> None:
        self.auth_sync_service = auth_sync_service
        self.fetcher = ChatGPTResetCreditFetcher()
        self._stop_event = Event()
        self._thread: Thread | None = None
        self._retry_thread: Thread | None = None
        self._lock = Lock()
        self._items: dict[str, ResetCreditState] = {}
        self._request_locks: dict[str, Lock] = {}
        self._proxy_provider: Callable[[], str] | None = None
        self._on_change: Callable[[], None] | None = None
        self._auto_use_enabled = Event()
        self._auto_attempted: dict[tuple[str, str], float] = {}
        self._auto_pending: dict[str, AutoResetCreditAttempt] = {}
        self._on_auto_use: Callable[[], None] | None = None

    def set_auto_use_enabled(self, enabled: bool) -> None:
        if enabled:
            self._auto_use_enabled.set()
        else:
            self._auto_use_enabled.clear()

    def set_auto_use_callback(self, callback: Callable[[], None]) -> None:
        self._on_auto_use = callback

    def set_proxy_provider(self, provider: Callable[[], str]) -> None:
        self._proxy_provider = provider

    def set_change_callback(self, callback: Callable[[], None]) -> None:
        self._on_change = callback

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = Thread(target=self._run, daemon=True, name="auth-reset-credits")
        self._thread.start()
        self._retry_thread = Thread(target=self._run_auto_retry, daemon=True, name="reset-credit-retry")
        self._retry_thread.start()

    def stop(self) -> None:
        self._stop_event.set()

    def item_for(self, refresh_token: str) -> ResetCreditState | None:
        with self._lock:
            return self._items.get(refresh_token)

    @staticmethod
    def earliest_expiring_credit(info: ResetCreditInfo) -> ResetCredit | None:
        if info.available_count <= 0:
            return None
        now = time.time()
        return min(
            (credit for credit in info.credits if credit.expires_at is None or credit.expires_at > now),
            key=lambda credit: (
                credit.expires_at if credit.expires_at is not None else float("inf"),
                credit.granted_at, credit.credit_id,
            ), default=None,
        )

    def _request_lock_for(self, row: AuthFileRow) -> Lock:
        with self._lock:
            return self._request_locks.setdefault(row.account_id or row.refresh_token, Lock())

    def _current_row(self, row: AuthFileRow) -> AuthFileRow:
        for current in self.auth_sync_service.list_auth_rows():
            if current.account_id == row.account_id and current.file_name == row.file_name:
                return current
        raise ValueError("该账户已被删除，请重新选择")

    def _fetch_current(self, row: AuthFileRow) -> ResetCreditInfo:
        proxy_url = self._proxy_provider() if self._proxy_provider else ""
        info = self.fetcher.fetch(row.access_token, row.account_id, proxy_url)
        with self._lock:
            self._items[row.refresh_token] = ResetCreditState(info)
        if self._on_change:
            self._on_change()
        return info

    def prepare_use(self, row: AuthFileRow) -> ResetCredit:
        with self._request_lock_for(row):
            current = self._current_row(row)
            credit = self.earliest_expiring_credit(self._fetch_current(current))
            if credit is None:
                raise ValueError("该账户没有可用的重置卡")
            return credit

    def use_credit(self, row: AuthFileRow, credit_id: str, redeem_request_id: str) -> str:
        with self._request_lock_for(row):
            current = self._current_row(row)
            credit = self.earliest_expiring_credit(self._fetch_current(current))
            if credit is None or credit.credit_id != credit_id:
                raise ValueError("最早到期的重置卡已变化或失效，请重新点击并确认")
            proxy_url = self._proxy_provider() if self._proxy_provider else ""
            try:
                return self.fetcher.consume(current.access_token, current.account_id, proxy_url,
                                            credit_id, redeem_request_id)
            finally:
                # 使用后的读取失败不能覆盖使用结果，也不能自动重试使用请求。
                try:
                    self._fetch_current(current)
                except Exception:
                    with self._lock:
                        previous = self._items.get(current.refresh_token)
                        self._items[current.refresh_token] = ResetCreditState(
                            previous.info if previous else None, failed=True)
                    if self._on_change:
                        self._on_change()

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.refresh_once()
            except Exception as exc:
                print(f"[ResetCredits] 后台刷新失败: {type(exc).__name__}", flush=True)
            if self._stop_event.wait(random.uniform(10.0, 30.0)):
                return

    def _run_auto_retry(self) -> None:
        # 补偿独立于全量刷新，慢账号不会额外叠加整轮刷新等待时间。
        while not self._stop_event.wait(1.0):
            if not self._auto_use_enabled.is_set():
                continue
            try:
                self._retry_due_once()
            except Exception as exc:
                print(f"[ResetCredits] 自动用卡补偿调度失败: {type(exc).__name__}", flush=True)

    def _retry_due_once(self) -> None:
        if not self._auto_use_enabled.is_set() or self._stop_event.is_set():
            return
        now = time.monotonic()
        with self._lock:
            due = {account for account, attempt in self._auto_pending.items()
                   if attempt.retry_at <= now}
        rows = {}
        for row in self.auth_sync_service.list_auth_rows():
            account = row.account_id or row.refresh_token
            if account in due:
                rows.setdefault(account, row)
        proxy_url = self._proxy_provider() if self._proxy_provider else ""
        if rows:
            with ThreadPoolExecutor(max_workers=min(4, len(rows)), thread_name_prefix="reset-credit-retry") as executor:
                list(executor.map(lambda row: self._retry_row(row, proxy_url), rows.values()))

    def _retry_row(self, row: AuthFileRow, proxy_url: str) -> None:
        with self._request_lock_for(row):
            account = row.account_id or row.refresh_token
            with self._lock:
                attempt = self._auto_pending.get(account)
                if attempt is None or attempt.retry_at > time.monotonic():
                    return
            self._refresh_row_locked(row, proxy_url)
        if self._on_change:
            self._on_change()

    def refresh_once(self) -> None:
        rows = self.auth_sync_service.list_auth_rows()
        tokens = {row.refresh_token for row in rows}
        accounts = {row.account_id or row.refresh_token for row in rows}
        with self._lock:
            self._items = {key: value for key, value in self._items.items() if key in tokens}
            self._auto_pending = {key: value for key, value in self._auto_pending.items() if key in accounts}
            self._auto_attempted = {key: expiry for key, expiry in self._auto_attempted.items()
                                    if expiry > time.time()}
        proxy_url = self._proxy_provider() if self._proxy_provider else ""
        if rows:
            with ThreadPoolExecutor(max_workers=min(4, len(rows)), thread_name_prefix="reset-credit") as executor:
                list(executor.map(lambda row: self._refresh_row(row, proxy_url), rows))
        if not self._stop_event.is_set() and self._on_change:
            self._on_change()

    def _refresh_row(self, row, proxy_url: str) -> None:
        with self._request_lock_for(row):
            self._refresh_row_locked(row, proxy_url)

    def _refresh_row_locked(self, row, proxy_url: str) -> None:
        if self._stop_event.is_set():
            return
        try:
            info = self.fetcher.fetch(row.access_token, row.account_id, proxy_url)
            state = ResetCreditState(info)
        except Exception:
            with self._lock:
                previous = self._items.get(row.refresh_token)
                pending = self._auto_pending.get(row.account_id or row.refresh_token)
                if pending is not None:
                    pending.retry_at = time.monotonic() + random.uniform(_AUTO_RETRY_MIN_SECONDS, _AUTO_RETRY_MAX_SECONDS)
            state = ResetCreditState(previous.info if previous else None, failed=True)
        with self._lock:
            self._items[row.refresh_token] = state
        if state.info is not None and not state.failed:
            for _ in state.info.credits:
                if not self._auto_use_expiring_credit(row, state.info):
                    break
                with self._lock:
                    state = self._items.get(row.refresh_token, state)
                if state.info is None or state.failed:
                    break

    def _auto_use_expiring_credit(self, row: AuthFileRow, info: ResetCreditInfo) -> bool:
        if not self._auto_use_enabled.is_set() or self._stop_event.is_set():
            return False
        now = time.time()
        account = row.account_id or row.refresh_token
        with self._lock:
            pending = self._auto_pending.get(account)
            if pending is not None:
                # 查询成功后卡已不在可用列表，是服务端确认不可用；本地时间不作终止依据。
                if info.available_count <= 0 or not any(credit.credit_id == pending.credit.credit_id for credit in info.credits):
                    self._auto_attempted[(account, pending.credit.credit_id)] = pending.credit.expires_at
                    del self._auto_pending[account]
                    return False
                if pending.retry_at > time.monotonic():
                    return False
                credit = pending.credit
            else:
                credit = min((credit for credit in info.credits
                          if info.available_count > 0 and credit.expires_at is not None
                          and 0 < credit.expires_at - now <= _AUTO_USE_WINDOW_SECONDS
                          and (account, credit.credit_id) not in self._auto_attempted),
                         key=lambda credit: (credit.expires_at, credit.granted_at, credit.credit_id),
                         default=None)
        if credit is None:
            return False
        key = (account, credit.credit_id)
        try:
            current = self._current_row(row)
            if not self._auto_use_enabled.is_set() or self._stop_event.is_set():
                return False
            if pending is None and credit.expires_at <= time.time():
                return False
            with self._lock:
                self._auto_pending[account] = AutoResetCreditAttempt(credit, float("inf"))
            # 每次补偿复用同一卡和请求标识，响应丢失时不切换下一张卡。
            request_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"reset-credit:{key[0]}:{key[1]}"))
            proxy_url = self._proxy_provider() if self._proxy_provider else ""
            code = self.fetcher.consume(current.access_token, current.account_id, proxy_url,
                                        credit.credit_id, request_id)
            if code in _AUTO_USE_TERMINAL_RESULTS:
                with self._lock:
                    self._auto_attempted[key] = credit.expires_at
                    self._auto_pending.pop(account, None)
            print(f"[ResetCredits] 到期自动用卡结果: {code}", flush=True)
        except Exception as exc:
            print(f"[ResetCredits] 到期自动用卡失败: {type(exc).__name__}", flush=True)
        finally:
            with self._lock:
                attempt = self._auto_pending.get(account)
                if attempt is not None:
                    attempt.retry_at = time.monotonic() + random.uniform(_AUTO_RETRY_MIN_SECONDS, _AUTO_RETRY_MAX_SECONDS)
            try:
                self._fetch_current(row)
            except Exception:
                with self._lock:
                    previous = self._items.get(row.refresh_token)
                    self._items[row.refresh_token] = ResetCreditState(
                        previous.info if previous else None, failed=True)
            if self._on_auto_use:
                self._on_auto_use()
        return True
