from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
from collections import Counter
from pathlib import Path
from threading import RLock

from app.models import LoadStrategy
from app.services.auth_cooldown_service import AuthCooldownService
from app.services.auth_sync_service import AuthFileRow
from app.utils.path_utils import app_root


class ConnectionLoadService:
    """按账号分配连接租约；选择和计数在同一把锁内完成。"""

    _PRIORITY_WEIGHT = 1.25
    _NORMAL_WEIGHT = 1.0

    def __init__(self, cooldown: AuthCooldownService, affinity_path: Path | None = None, clock=time.monotonic) -> None:
        self._lock = RLock()
        self._clock = clock
        self._last_seen = None
        self._state_sequence = -1
        self._cooldown = cooldown
        self._rows: list[AuthFileRow] = []
        self._enabled = False
        self._session = ""
        self._leases: dict[str, tuple[str, AuthFileRow]] = {}
        self._counts: Counter[str] = Counter()
        self._activity: dict[str, tuple[int, int]] = {}
        self._active_counts: Counter[str] = Counter()
        self._last_selected: dict[str, int] = {}
        self._sequence = 0
        self._affinity_path = affinity_path or app_root() / "data" / "connection_affinity.json"
        self._affinity_load_failed = False
        self._affinities: dict[str, dict[str, object]] = self._load_affinities()

    @staticmethod
    def key(row: AuthFileRow) -> str:
        # 同一账户的多份授权共用计数，令牌刷新也不改变归属。
        return row.account_id or row.file_name or hashlib.sha256(row.refresh_token.encode("utf-8")).hexdigest()

    def configure(self, rows: list[AuthFileRow], enabled: bool) -> None:
        with self._lock:
            self._rows = list(rows)
            self._enabled = enabled

    def _load_affinities(self) -> dict[str, dict[str, object]]:
        try:
            data = json.loads(self._affinity_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError, UnicodeError):
            self._affinity_load_failed = True
            return {}
        bindings = data.get("bindings") if isinstance(data, dict) else None
        if (not isinstance(bindings, dict) or data.get("version") != 1
                or any(not isinstance(key, str) or not isinstance(value, dict)
                       or not isinstance(value.get("account_key"), str) or not value["account_key"]
                       for key, value in bindings.items())):
            self._affinity_load_failed = True
            return {}
        return dict(bindings)

    def _save_affinities(self) -> bool:
        temporary_path = None
        try:
            self._affinity_path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                    mode="w", encoding="utf-8", dir=self._affinity_path.parent,
                    prefix=self._affinity_path.name + ".", suffix=".tmp", delete=False) as output:
                temporary_path = Path(output.name)
                json.dump({"version": 1, "bindings": self._affinities}, output, ensure_ascii=False, indent=2)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary_path, self._affinity_path)
            return True
        except OSError:
            return False
        finally:
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass

    def open_session(self, session: str) -> bool:
        with self._lock:
            changed = session != self._session
            if changed:
                self.clear()
                self._session = session
                self._last_seen = self._clock()
            return changed

    def clear(self) -> None:
        with self._lock:
            self._leases.clear()
            self._counts.clear()
            self._activity.clear()
            self._active_counts.clear()
            self._last_selected.clear()
            self._sequence = 0
            self._state_sequence = -1
            self._last_seen = None

    def _find_bound_row(self, affinity_key: str) -> tuple[dict[str, object], AuthFileRow | None] | None:
        binding = self._affinities.get(affinity_key)
        if not isinstance(binding, dict):
            return None
        account_key = str(binding.get("account_key") or "")
        rows = [item for item in self._rows if self.key(item) == account_key]
        row = next((item for item in rows if self._usable(item)), rows[0] if rows else None)
        return binding, row

    def _usable(self, row: AuthFileRow) -> bool:
        return not self._exclusion_reason(row)

    def _exclusion_reason(self, row: AuthFileRow) -> str:
        percentages = self._quota_percentages(row)
        if row.disabled or row.load_strategy == LoadStrategy.DISABLED:
            return "disabled"
        if not row.access_token:
            return "missing_token"
        if not percentages:
            return "quota_unknown"
        if not all(value > 0 for value in percentages):
            return "quota_exhausted"
        if self._cooldown.remaining(self._cooldown.key(row)):
            return "cooldown"
        return ""

    @staticmethod
    def _quota_percentages(row: AuthFileRow) -> tuple[float, float]:
        values = [float(value) for value in re.findall(r"(\d+(?:\.\d+)?)%", row.quota)]
        if not values:
            return 0.0, 0.0
        first = max(0.0, min(100.0, values[0]))
        second = max(0.0, min(100.0, values[1] if len(values) > 1 else values[0]))
        return first, second

    def _logical_load(self, key: str) -> int:
        # 空闲 WS 算一个会话；同一 WS 上并行 stream 超过一条时，按 stream 数扩展负载。
        return max(self._counts[key], self._active_counts[key])

    def _strategy_weight(self, row: AuthFileRow) -> float:
        return self._PRIORITY_WEIGHT if row.load_strategy == LoadStrategy.PRIORITY else self._NORMAL_WEIGHT

    def _selection_score(self, row: AuthFileRow) -> tuple[float, int, float, float, int, str, str]:
        five_hour, seven_day = self._quota_percentages(row)
        # 额度越多，单位线程的有效负载越低；优先策略是加权偏好而不是阻塞正常账号。
        score = (self._logical_load(self.key(row)) + 1.0) / max(five_hour, 1.0) / self._strategy_weight(row)
        return (
            score,
            0 if row.load_strategy == LoadStrategy.PRIORITY else 1,
            -five_hour,
            -seven_day,
            self._last_selected.get(self.key(row), 0),
            self.key(row),
            row.file_name,
        )

    def diagnostics(self) -> dict:
        with self._lock:
            reasons = Counter(self._exclusion_reason(row) or "eligible" for row in self._rows)
            return {"rows": len(self._rows), "eligibility": dict(reasons), "health": self.health(),
                    "leases": len(self._leases), "bindings": len(self._affinities),
                    "load_units": self.load_units()}

    def health(self) -> str:
        with self._lock:
            if self._last_seen is None:
                return "waiting"
            return "ok" if self._clock() - self._last_seen <= 5 else "stale"

    def _candidates(self) -> list[AuthFileRow]:
        eligible = [row for row in self._rows if self._usable(row)]
        # 禁用/冷却/额度不足硬排除；优先账号使用加权偏好，不再让正常账号长期闲置。
        return eligible

    def _remember_affinity(self, affinity_key: str, binding: dict, payload: dict) -> bool:
        if len(self._affinities) >= 10000:
            payload.update(affinity_unavailable=True, error_code="autoload_affinity_capacity")
            return False
        self._affinities[affinity_key] = binding
        if self._save_affinities():
            return True
        self._affinities.pop(affinity_key, None)
        payload.update(affinity_unavailable=True, error_code="autoload_affinity_storage_error")
        return False

    def select(self, lease_id: str = "", session: str = "", affinity_key: str = "",
               preserve_original: bool = False, initial_active: bool = False) -> dict:
        with self._lock:
            payload = {"auto_load": self._enabled, "access_token": "", "account_id": ""}
            if affinity_key and self._affinity_load_failed and self._enabled:
                payload.update(affinity_unavailable=True, error_code="autoload_affinity_storage_error")
                return payload
            if lease_id and (not session or session != self._session):
                payload["error_code"] = "autoload_session_mismatch"
                return payload
            bound = self._find_bound_row(affinity_key) if affinity_key else None
            if not self._enabled and bound is None:
                if affinity_key:
                    self._remember_affinity(affinity_key, {"account_key": "@original"}, payload)
                return payload
            existing = self._leases.get(lease_id) if lease_id else None
            if existing is not None:
                key = existing[0]
                row = next((item for item in self._rows if self.key(item) == key), existing[1])
            else:
                if bound is not None:
                    binding, row = bound
                    if binding.get("account_key") == "@original":
                        payload["auto_load"] = False
                        return payload
                    payload["auto_load"] = True
                    if row is None or not self._usable(row):
                        payload.update(
                            affinity_bound=True,
                            affinity_unavailable=True,
                            account_id=str(binding.get("account_id") or ""),
                            error_code="autoload_bound_account_unavailable",
                            reason=self._exclusion_reason(row) if row else "account_removed",
                        )
                        return payload
                else:
                    if affinity_key and preserve_original:
                        if self._remember_affinity(affinity_key, {"account_key": "@original"}, payload):
                            payload["auto_load"] = False
                        return payload
                    rows = self._candidates()
                    if not rows:
                        payload.update(error_code="autoload_no_candidates", diagnostics=self.diagnostics())
                        return payload
                    row = min(rows, key=self._selection_score)
                    if affinity_key:
                        binding = {
                            "account_key": self.key(row),
                            "account_id": row.account_id,
                            "updated_at": time.time(),
                        }
                        if not self._remember_affinity(affinity_key, binding, payload):
                            return payload
                if lease_id:
                    key = self.key(row)
                    self._counts[key] += 1
                    self._sequence += 1
                    self._last_selected[key] = self._sequence
                    self._leases[lease_id] = (key, row)
                    self._activity[lease_id] = (0, int(initial_active))
                    self._active_counts[key] += int(initial_active)
            payload.update(access_token=row.access_token, account_id=row.account_id, account_key=self.key(row))
            if lease_id:
                payload["lease_id"] = lease_id
            if affinity_key:
                payload["affinity_key"] = affinity_key
            return payload

    def reconcile(self, session: str, sequence: int, leases: list[dict]) -> dict:
        """完整快照用于对账；发送端必须与分配操作互斥，旧快照不覆盖新状态。"""
        with self._lock:
            if session != self._session:
                return {"error_code": "autoload_session_mismatch"}
            if sequence <= self._state_sequence:
                return {"ok": True}
            if len(leases) > 10000:
                return {"error_code": "autoload_snapshot_invalid"}
            rows = {self.key(row): row for row in self._rows}
            restored = {}
            for item in leases:
                if not isinstance(item, dict):
                    return {"error_code": "autoload_snapshot_invalid"}
                lease_id, key, affinity = item.get("id"), item.get("account"), item.get("affinity")
                revision, count = item.get("revision"), item.get("active")
                binding = self._affinities.get(affinity) if isinstance(affinity, str) else None
                if (not isinstance(lease_id, str) or not lease_id.startswith(session + ":")
                        or not isinstance(key, str) or not isinstance(revision, int) or revision < 0
                        or not isinstance(count, int) or not 0 <= count <= 1024
                        or not binding or binding.get("account_key") != key or lease_id in restored):
                    return {"error_code": "autoload_snapshot_invalid"}
                existing = self._leases.get(lease_id)
                row = rows.get(key) or (existing[1] if existing and existing[0] == key else None)
                if row is None:
                    return {"error_code": "autoload_snapshot_account_missing"}
                restored[lease_id] = (key, row, revision, count)
            for lease_id in set(self._leases) - restored.keys():
                self.release(lease_id)
            for lease_id, (key, row, revision, count) in restored.items():
                if lease_id not in self._leases:
                    self._leases[lease_id] = (key, row)
                    self._counts[key] += 1
                    self._activity[lease_id] = (-1, 0)
                self.set_activity(lease_id, revision, count)
            self._state_sequence = sequence
            self._last_seen = self._clock()
            return {"ok": True}

    def set_activity(self, lease_id: str, revision: int, count: int) -> None:
        with self._lock:
            lease = self._leases.get(lease_id)
            if lease is None or revision <= self._activity[lease_id][0] or not 0 <= count <= 1024:
                return
            key = lease[0]
            previous = self._activity[lease_id][1]
            self._activity[lease_id] = (revision, count)
            self._active_counts[key] += count - previous
            if self._active_counts[key] <= 0:
                self._active_counts.pop(key, None)

    def release(self, lease_id: str) -> None:
        with self._lock:
            lease = self._leases.pop(lease_id, None)
            if lease is not None:
                key = lease[0]
                self._active_counts[key] -= self._activity.pop(lease_id, (0, 0))[1]
                if self._active_counts[key] <= 0:
                    self._active_counts.pop(key, None)
                self._counts[key] -= 1
                if self._counts[key] <= 0:
                    self._counts.pop(key, None)

    def counts(self) -> dict[str, int]:
        with self._lock:
            return {row.refresh_token: self._counts[self.key(row)] for row in self._rows}

    def statistics(self) -> tuple[dict[str, int], dict[str, int]]:
        with self._lock:
            return self.counts(), {row.refresh_token: self._active_counts[self.key(row)] for row in self._rows}

    def load_units(self) -> dict[str, int]:
        with self._lock:
            return {row.refresh_token: self._logical_load(self.key(row)) for row in self._rows}

    def rate_limit(self, access_token: str) -> tuple[str, int] | None:
        with self._lock:
            rows = self._rows + [lease[1] for lease in self._leases.values()]
            row = next((item for item in rows if item.access_token == access_token), None)
            if row is None:
                return None
            seconds = self._cooldown.hit(self._cooldown.key(row))
            return row.account_id, seconds
