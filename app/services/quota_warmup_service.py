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
OBSERVATION_INTERVAL = 45
RESET_TOLERANCE = 15
RETRY_INTERVAL = 300
MAX_CYCLE_ATTEMPTS = 2


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
        # 仅筛选候选；是否未启动必须通过连续观察确认，不能依赖百分比。
        return first.reset_at <= now or (
            abs(first.reset_at - now - first.duration_seconds) <= 90
        )

    @staticmethod
    def _key(row) -> str:
        return row.account_id or row.file_name

    def status_for(self, row) -> str:
        with self._lock:
            state = dict(self._attempts.get(self._key(row), {}))
        lines = [state.get("status", "等待窗口观察")]
        for field, label in (("last_sent_at", "最近发送"), ("confirmed_at", "最近确认")):
            if state.get(field):
                lines.append(label + ": " + time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(state[field])))
        return "\n".join(lines)

    def _record(self, key: str, model: str, until: float, status: str, attempted: bool, **fields) -> None:
        with self._lock:
            state = dict(self._attempts.get(key, {}))
            state.update(model=model, until=until, status=status, attempted=attempted, **fields)
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.state_path.with_suffix(".tmp")
            temporary.write_text(json.dumps({**self._attempts, key: state}, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(self.state_path)
            self._attempts[key] = state
        if self._on_change:
            try:
                self._on_change()
            except Exception as exc:
                print(f"[QuotaWarmup] 状态回调失败: {type(exc).__name__}", flush=True)

    def _observe(self, key: str, model: str, windows: tuple[QuotaWindow, ...], observed_at: float) -> dict:
        with self._lock:
            state = dict(self._attempts.get(key, {}))
        if not windows:
            return state
        if state.get("version") != 2:
            # 旧记录没有窗口证据，尚在保护期内的发送按结果不确定处理。
            protected = state.get("attempted", False) and state.get("until", 0) > observed_at
            state.update(version=2, phase="uncertain" if protected else "observing",
                         count=MAX_CYCLE_ATTEMPTS if protected else 0, cycle=1,
                         observation={}, cycle_reset_at=0, rolling_since=0,
                         status="旧预热记录结果不确定，继续观察" if protected else "观察窗口中")
        previous = state.get("observation", {})
        elapsed = observed_at - previous.get("at", observed_at)
        if previous and elapsed < OBSERVATION_INTERVAL:
            return state
        first = windows[0]
        # 只有已确认启动的窗口确实到期，才能开启下一周期的发送预算。
        boundary = state.get("cycle_reset_at", 0)
        if boundary and observed_at >= boundary and first.reset_at > boundary + RESET_TOLERANCE:
            state.update(cycle=state["cycle"] + 1, count=0, phase="observing", until=0,
                         attempted=False, cycle_reset_at=0, rolling_since=0, confirmed_at=0,
                         last_sent_at=0, status="新周期观察中")
        comparable = previous and previous.get("duration") == first.duration_seconds
        stable = (comparable and first.reset_at > observed_at
                  and abs(first.reset_at - previous["reset_at"]) <= RESET_TOLERANCE)
        rolling = (comparable and elapsed <= 180
                   and abs(first.reset_at - previous["reset_at"] - elapsed) <= RESET_TOLERANCE
                   and abs(previous["reset_at"] - previous["at"] - first.duration_seconds) <= 90
                   and self.ready(windows, observed_at))
        if stable:
            state.update(phase="active", cycle_reset_at=first.reset_at, until=first.reset_at,
                         confirmed_at=state.get("confirmed_at") or observed_at, rolling_since=0,
                         status="已确认，窗口已启动，无需重复预热")
        elif rolling:
            state["rolling_since"] = state.get("rolling_since") or max(previous["at"], state.get("last_sent_at", 0))
            if state["phase"] in {"observing", "eligible"}:
                state.update(phase="eligible", status="窗口持续滑动，等待预热")
            elif (state["phase"] == "waiting" and state.get("count", 0) >= MAX_CYCLE_ATTEMPTS
                  and observed_at >= state.get("until", 0)):
                state.update(phase="exhausted", status="预热未启动周期，本周期停止发送")
        else:
            state["rolling_since"] = 0
            if state["phase"] == "eligible":
                state.update(phase="observing", status="窗口变化尚不明确，继续观察")
        state["observation"] = {
            "at": observed_at, "reset_at": first.reset_at, "duration": first.duration_seconds,
            "windows": [{"reset_at": w.reset_at, "duration": w.duration_seconds,
                         "remaining": w.remaining_percent} for w in windows],
        }
        fields = {k: v for k, v in state.items() if k not in {"model", "until", "status", "attempted"}}
        self._record(key, state.get("model", model), state.get("until", 0), state["status"],
                     state.get("attempted", False), **fields)
        return state

    @staticmethod
    def _can_send(state: dict, model: str, now: float) -> bool:
        if state.get("count", 0) >= MAX_CYCLE_ATTEMPTS or not state.get("rolling_since"):
            return False
        phase = state.get("phase")
        if phase == "backoff":
            return state.get("until", 0) <= now or state.get("model") != model
        if phase == "waiting":
            return now >= state.get("until", 0) and state["rolling_since"] >= state.get("last_sent_at", 0)
        return phase == "eligible"

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
                state = self._observe(key, model, item.windows, item.observed_at)
                if not self.ready(item.windows, item.observed_at) or not self._can_send(state, model, now):
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
            observed_at = time.time()
            if not windows:
                raise RuntimeError("额度接口未返回有效窗口")
            state = self._observe(key, model, windows, observed_at)
            baseline = state.get("observation", {})
            still_rolling = (baseline.get("duration") == windows[0].duration_seconds
                             and abs(windows[0].reset_at - baseline.get("reset_at", 0)
                                     - (observed_at - baseline.get("at", observed_at))) <= RESET_TOLERANCE)
            limits = (result.raw or {}).get("rate_limit") or {}
            if (not still_rolling or not self.ready(windows, observed_at) or not self._can_send(state, model, observed_at)
                    or limits.get("allowed") is False or limits.get("limit_reached") is True):
                return
            self.client.check_model(row.access_token, row.account_id, proxy, model)
            current = next((item for item in self.auth.list_auth_rows() if self._key(item) == key), None)
            with self._lock:
                active = self._enabled and self._generation == generation
            if (not active or self._stop.is_set() or current is None or current.disabled
                    or current.load_strategy == LoadStrategy.DISABLED or current.access_token != access_token):
                return
            sent_at = time.time()
            until = sent_at + windows[0].duration_seconds
            # 先持久化再发送，避免程序重启、请求超时导致重复消耗。
            self._record(key, model, until, "预热请求发送中，结果待确认", True,
                         phase="uncertain", count=state.get("count", 0) + 1,
                         last_sent_at=sent_at, rolling_since=0)
            attempted = True
            try:
                self.client.ping(row.access_token, row.account_id, proxy, model, self._stop)
            finally:
                self.auth.increment_traffic_by_access_token(row.access_token)
            self._record(key, model, sent_at + RETRY_INTERVAL, "请求完成，等待窗口启动确认", True,
                         phase="waiting")
        except WarmupRejected as exc:
            self._record(key, model, time.time() + 3600, str(exc), attempted, phase="backoff")
        except Exception as exc:
            status = "预热结果不确定，继续观察并暂停重发" if attempted else "检查失败，15分钟后重试"
            self._record(key, model, until, status + f"（{type(exc).__name__}）", attempted,
                         phase="uncertain" if attempted else "backoff")
        finally:
            self.usage.request_refresh()
