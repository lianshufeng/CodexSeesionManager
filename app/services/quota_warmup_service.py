from __future__ import annotations

import json
import time
from pathlib import Path
from threading import Event, Lock, Thread
from typing import Callable

from app.models import LoadStrategy, QuotaWindow
from app.services.auth_sync_service import AuthSyncService
from app.services.auth_usage_service import AuthUsageService
from app.utils.chatgpt_usage_fetcher import ChatGPTUsageFetcher
from app.utils.codex_warmup_client import CodexWarmupClient, WarmupRejected
from app.utils.path_utils import app_root


DEFAULT_WARMUP_MODEL = "gpt-6-luna"


class QuotaWarmupService:
    def __init__(self, auth: AuthSyncService, usage: AuthUsageService, *,
                 client=None, fetcher=None, state_path: Path | None = None) -> None:
        self.auth = auth
        self.usage = usage
        self.client = client or CodexWarmupClient()
        self.fetcher = fetcher or ChatGPTUsageFetcher()
        self.state_path = state_path or app_root() / "data" / "quota_warmup.json"
        self._stop = Event()
        self._lock = Lock()
        self._check_lock = Lock()
        self._thread = None
        self._enabled = False
        self._model = DEFAULT_WARMUP_MODEL
        self._generation = 0
        self._proxy_provider: Callable[[], str] = lambda: ""
        self._on_change: Callable[[], None] | None = None
        self._attempts = {}
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                self._attempts = {key: value for key, value in data.items() if isinstance(value, dict)}
        except (OSError, ValueError):
            pass

    def configure(self, enabled: bool, model: str) -> None:
        with self._lock:
            model = model.strip()
            if (enabled, model) != (self._enabled, self._model):
                self._generation += 1
            self._enabled, self._model = enabled, model

    def set_proxy_provider(self, provider: Callable[[], str]) -> None:
        self._proxy_provider = provider

    def set_change_callback(self, callback: Callable[[], None]) -> None:
        self._on_change = callback

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = Thread(target=self._run, daemon=True, name="quota-warmup")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.wait(60):
            try:
                self.check_once()
            except Exception as exc:
                print(f"[QuotaWarmup] 检查失败: {type(exc).__name__}", flush=True)

    @staticmethod
    def ready(windows: tuple[QuotaWindow, ...], now: float) -> bool:
        if not windows or windows[0].remaining_percent < 99:
            return False
        if any(window.remaining_percent <= 0 for window in windows):
            return False
        first = windows[0]
        # 未使用的窗口可能每次查询都返回“现在 + 完整周期”。
        return first.reset_at <= now or (
            first.remaining_percent == 100
            and first.duration_seconds - 90 <= first.reset_at - now <= first.duration_seconds + 90
        )

    @staticmethod
    def _key(row) -> str:
        return row.account_id or row.file_name

    def status_for(self, row) -> str:
        with self._lock:
            return self._attempts.get(self._key(row), {}).get("status", "等待窗口恢复")

    def _record(self, key: str, model: str, until: float, status: str, attempted: bool) -> None:
        with self._lock:
            self._attempts[key] = {"model": model, "until": until, "status": status, "attempted": attempted}
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.state_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(self._attempts, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(self.state_path)
        if self._on_change:
            try:
                self._on_change()
            except Exception as exc:
                print(f"[QuotaWarmup] 状态回调失败: {type(exc).__name__}", flush=True)

    def check_once(self) -> None:
        if not self._check_lock.acquire(blocking=False):
            return
        try:
            with self._lock:
                enabled, model, generation = self._enabled, self._model, self._generation
            if not enabled or not model or self._stop.is_set():
                return
            for row in self.auth.list_auth_rows():
                if self._stop.is_set():
                    return
                if row.disabled or row.load_strategy == LoadStrategy.DISABLED or not row.access_token:
                    continue
                item = self.usage.item_for(row.refresh_token)
                now = time.time()
                if item is None or not item.windows or not 0 <= now - item.observed_at <= 90:
                    continue
                key = self._key(row)
                with self._lock:
                    attempt = dict(self._attempts.get(key, {}))
                if attempt.get("until", 0) > now and (attempt.get("attempted") or attempt.get("model") == model):
                    if attempt.get("attempted") and "确认" in attempt.get("status", "") and not self.ready(item.windows, now):
                        self._record(key, model, item.windows[0].reset_at, "预热完成，窗口已启动", True)
                    continue
                if not self.ready(item.windows, now):
                    continue
                self._warmup(row, key, model, generation)
        finally:
            self._check_lock.release()

    def _warmup(self, row, key: str, model: str, generation: int) -> None:
        now = time.time()
        access_token = row.access_token
        attempted = False
        until = now + 900
        try:
            proxy = self._proxy_provider()
            result = self.fetcher.fetch(row.access_token, row.account_id, proxy_url=proxy)
            windows = result.windows
            limits = (result.raw or {}).get("rate_limit") or {}
            if not self.ready(windows, time.time()) or limits.get("allowed") is False or limits.get("limit_reached") is True:
                return
            self.client.check_model(row.access_token, row.account_id, proxy, model)
            current = next((item for item in self.auth.list_auth_rows() if self._key(item) == key), None)
            with self._lock:
                active = self._enabled and self._generation == generation
            if (not active or self._stop.is_set() or current is None or current.disabled
                    or current.load_strategy == LoadStrategy.DISABLED or current.access_token != access_token):
                return
            until = time.time() + windows[0].duration_seconds
            # 先持久化再发送，避免程序重启、请求超时导致重复消耗。
            self._record(key, model, until, "预热请求发送中", True)
            attempted = True
            try:
                self.client.ping(row.access_token, row.account_id, proxy, model, self._stop)
            finally:
                self.auth.increment_traffic_by_access_token(row.access_token)
            refreshed = self.fetcher.fetch(row.access_token, row.account_id, proxy_url=proxy)
            new_windows = refreshed.windows
            confirmed = bool(new_windows and new_windows[0].reset_at > time.time() and (
                windows[0].reset_at <= now < new_windows[0].reset_at
                or new_windows[0].remaining_percent < windows[0].remaining_percent
                or not self.ready(new_windows, time.time())
            ))
            if confirmed:
                until = new_windows[0].reset_at
            self._record(key, model, until, "预热完成，窗口已启动" if confirmed else "请求完成，等待接口确认窗口启动", True)
        except WarmupRejected as exc:
            self._record(key, model, time.time() + 3600, str(exc), False)
        except Exception as exc:
            status = "预热结果待确认，本周期暂停重发" if attempted else "检查失败，15分钟后重试"
            self._record(key, model, until, status + f"（{type(exc).__name__}）", attempted)
        finally:
            self.usage.request_refresh()
