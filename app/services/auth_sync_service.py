from __future__ import annotations

import json
import os
import hashlib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import traceback
from threading import Event, Lock, Thread
from typing import Callable

from app.utils.path_utils import app_root


_MANAGER_METADATA_KEY = "_codex_session_manager"
_LOAD_STRATEGY_NORMAL = "normal"
_LOAD_STRATEGY_PRIORITY = "priority"
_LOAD_STRATEGY_DISABLED = "disabled"
_LOAD_STRATEGIES = {
    _LOAD_STRATEGY_NORMAL,
    _LOAD_STRATEGY_PRIORITY,
    _LOAD_STRATEGY_DISABLED,
}
_FALLBACK_HASH_LENGTH = 32


@dataclass
class AuthFileRow:
    account_id: str
    refresh_token: str
    last_refresh: str
    user_id: str = ""
    email: str = ""
    plan_type: str = ""
    quota: str = ""
    quota_refresh_time_5h: str = ""
    quota_refresh_time_7d: str = ""
    traffic: int = 0
    current: bool = False
    disabled: bool = False
    load_strategy: str = _LOAD_STRATEGY_NORMAL
    note: str = ""
    file_name: str = ""
    access_token: str = ""


class AuthSyncService:
    def __init__(
        self,
        source_path: Path | None = None,
        target_dir: Path | None = None,
        interval_seconds: float = 1.0,
    ) -> None:
        user_profile = os.environ.get("USERPROFILE") or str(Path.home())
        self.source_path = source_path or Path(user_profile) / ".codex" / "auth.json"
        self.target_dir = target_dir or app_root() / "auth"
        self.interval_seconds = interval_seconds
        self._stop_event = Event()
        self._state_lock = Lock()
        self._thread: Thread | None = None
        self._on_change: Callable[[], None] | None = None
        self._source_signature: tuple[int, int] | None = None
        self._last_notified_source_signature: tuple[int, int] | None = None
        self._source_state: dict[str, str] | None = None
        self._quota_by_refresh_token: dict[str, str] = {}
        self._plan_type_by_refresh_token: dict[str, str] = {}
        self._user_id_by_refresh_token: dict[str, str] = {}
        self._email_by_refresh_token: dict[str, str] = {}
        self._quota_refresh_time_5h_by_refresh_token: dict[str, str] = {}
        self._quota_refresh_time_7d_by_refresh_token: dict[str, str] = {}
        self._traffic_by_refresh_token: dict[str, int] = {}
        self._access_token_to_refresh_token: dict[str, str] = {}
        self._disabled_refresh_tokens: set[str] = set()

    def set_change_callback(self, callback: Callable[[], None] | None) -> None:
        self._on_change = callback

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()

    def invalidate_cached_state(self) -> None:
        with self._state_lock:
            self._source_signature = None
            self._source_state = None
            self._last_notified_source_signature = None
            self._access_token_to_refresh_token = {}

    def update_usage_cache(
        self,
        quota_by_refresh_token: dict[str, str],
        plan_type_by_refresh_token: dict[str, str],
        user_id_by_refresh_token: dict[str, str],
        email_by_refresh_token: dict[str, str],
        quota_refresh_time_5h_by_refresh_token: dict[str, str],
        quota_refresh_time_7d_by_refresh_token: dict[str, str],
    ) -> None:
        with self._state_lock:
            self._quota_by_refresh_token = dict(quota_by_refresh_token)
            self._plan_type_by_refresh_token = dict(plan_type_by_refresh_token)
            self._user_id_by_refresh_token = dict(user_id_by_refresh_token)
            self._email_by_refresh_token = dict(email_by_refresh_token)
            self._quota_refresh_time_5h_by_refresh_token = dict(quota_refresh_time_5h_by_refresh_token)
            self._quota_refresh_time_7d_by_refresh_token = dict(quota_refresh_time_7d_by_refresh_token)

    def increment_traffic_by_access_token(self, access_token: str) -> bool:
        token = access_token.strip()
        if not token:
            return False

        refresh_token = self._resolve_refresh_token_by_access_token(token)
        if not refresh_token:
            return False

        with self._state_lock:
            self._traffic_by_refresh_token[refresh_token] = self._traffic_by_refresh_token.get(refresh_token, 0) + 1
        return True

    def _resolve_refresh_token_by_access_token(self, access_token: str) -> str:
        with self._state_lock:
            refresh_token = self._access_token_to_refresh_token.get(access_token)
        if refresh_token:
            return refresh_token

        if not self.target_dir.exists():
            return ""

        for path in sorted(self.target_dir.glob("*.json"), key=lambda item: item.name):
            data = self._read_auth_data(path)
            if data is None:
                continue
            tokens = data.get("tokens")
            if not isinstance(tokens, dict):
                continue
            if str(tokens.get("access_token") or "") != access_token:
                continue
            refresh_token = str(tokens.get("refresh_token") or path.stem)
            with self._state_lock:
                self._access_token_to_refresh_token[access_token] = refresh_token
            return refresh_token
        return ""

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._sync_once()
            except Exception as exc:
                print(f"[AuthSync] 后台线程异常: {exc}\n{traceback.format_exc()}", flush=True)
            self._stop_event.wait(self.interval_seconds)

    def _get_file_signature(self, path: Path) -> tuple[int, int] | None:
        try:
            stat_result = path.stat()
        except OSError:
            return None
        return stat_result.st_mtime_ns, stat_result.st_size

    def _read_auth_data(self, path: Path) -> dict[str, object] | None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict):
            return None
        return data

    def _write_auth_data(self, path: Path, data: dict[str, object]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def _safe_auth_file_stem(self, account_id: str, refresh_token: str) -> str:
        stem = "".join(
            char if char.isalnum() or char in ("-", "_", ".") else "_"
            for char in account_id.strip()
        ).strip(" .")
        if stem:
            return stem
        digest = hashlib.sha256(refresh_token.encode("utf-8")).hexdigest()
        return f"rt_{digest[:_FALLBACK_HASH_LENGTH]}"

    def _auth_file_path(self, account_id: str, refresh_token: str) -> Path:
        return self.target_dir / f"{self._safe_auth_file_stem(account_id, refresh_token)}.json"

    def _find_auth_file_by_refresh_token(self, refresh_token: str) -> Path | None:
        if not self.target_dir.exists():
            return None
        for path in sorted(self.target_dir.glob("*.json"), key=lambda item: item.name):
            data = self._read_auth_data(path)
            if data is None:
                continue
            tokens = data.get("tokens")
            if not isinstance(tokens, dict):
                continue
            if str(tokens.get("refresh_token") or path.stem) == refresh_token:
                return path
        return None

    def _manager_metadata(self, data: dict[str, object]) -> dict[str, object]:
        metadata = data.get(_MANAGER_METADATA_KEY)
        return metadata if isinstance(metadata, dict) else {}

    def _load_strategy(self, data: dict[str, object]) -> str:
        metadata = self._manager_metadata(data)
        strategy = str(metadata.get("load_strategy") or "").strip()
        if strategy in _LOAD_STRATEGIES:
            return strategy
        if bool(metadata.get("disabled")):
            return _LOAD_STRATEGY_DISABLED
        return _LOAD_STRATEGY_NORMAL

    def _auth_note(self, data: dict[str, object]) -> str:
        return " ".join(str(self._manager_metadata(data).get("note") or "").split())

    def _strip_manager_metadata(self, data: dict[str, object]) -> dict[str, object]:
        cleaned = dict(data)
        cleaned.pop(_MANAGER_METADATA_KEY, None)
        return cleaned

    def _parse_last_refresh_timestamp(self, text: str) -> float:
        value = text.strip()
        if not value:
            return 0.0
        if value.endswith("Z"):
            value = f"{value[:-1]}+00:00"
        try:
            return datetime.fromisoformat(value).timestamp()
        except ValueError:
            return 0.0

    def _is_target_newer_than_source(self, target_last_refresh: str, source_last_refresh: str) -> bool:
        return self._parse_last_refresh_timestamp(target_last_refresh) > self._parse_last_refresh_timestamp(source_last_refresh)

    def _read_source_state(self, force: bool = False) -> dict[str, str] | None:
        signature = self._get_file_signature(self.source_path)
        if signature is None:
            return None

        with self._state_lock:
            if not force and signature == self._source_signature and self._source_state is not None:
                return self._source_state

        data = self._read_auth_data(self.source_path)
        if data is None:
            return None

        tokens = data.get("tokens")
        if not isinstance(tokens, dict):
            return None

        refresh_token = str(tokens.get("refresh_token") or "")
        access_token = str(tokens.get("access_token") or "")
        if not refresh_token or not access_token:
            return None

        state = {
            "account_id": str(data.get("account_id") or tokens.get("account_id") or ""),
            "refresh_token": refresh_token,
            "access_token": access_token,
            "last_refresh": str(data.get("last_refresh") or ""),
        }
        with self._state_lock:
            self._source_signature = signature
            self._source_state = state
        return state

    def _sync_once(self) -> None:
        file_signature = self._get_file_signature(self.source_path)
        if file_signature is None:
            return

        source_state = self._read_source_state()
        if source_state is None:
            return

        refresh_token = source_state["refresh_token"]
        content_signature = (
            source_state["account_id"],
            source_state["refresh_token"],
            source_state["access_token"],
            source_state["last_refresh"],
        )

        self.target_dir.mkdir(parents=True, exist_ok=True)
        target_path = self._auth_file_path(source_state["account_id"], refresh_token)
        legacy_path = self._find_auth_file_by_refresh_token(refresh_token)
        metadata_path = target_path if target_path.exists() else legacy_path
        target_state = None
        target_metadata: dict[str, object] = {}
        if metadata_path is not None and metadata_path.exists():
            target_data = self._read_auth_data(metadata_path)
            if target_data is not None:
                target_metadata = dict(self._manager_metadata(target_data))
                target_tokens = target_data.get("tokens")
                if isinstance(target_tokens, dict):
                    target_state = (
                        str(target_data.get("account_id") or target_tokens.get("account_id") or ""),
                        str(target_tokens.get("refresh_token") or ""),
                        str(target_tokens.get("access_token") or ""),
                        str(target_data.get("last_refresh") or ""),
                    )

        copied = False
        target_is_newer = (
            target_state is not None
            and target_state[1] == source_state["refresh_token"]
            and self._is_target_newer_than_source(target_state[3], source_state["last_refresh"])
        )
        if target_state != content_signature and not target_is_newer:
            source_data = self._read_auth_data(self.source_path)
            if source_data is not None:
                if target_metadata:
                    source_data[_MANAGER_METADATA_KEY] = target_metadata
                else:
                    source_data.pop(_MANAGER_METADATA_KEY, None)
                self._write_auth_data(target_path, source_data)
                if legacy_path is not None and legacy_path != target_path:
                    try:
                        legacy_path.unlink()
                    except OSError:
                        pass
                copied = True

        source_changed = file_signature != self._last_notified_source_signature
        if source_changed:
            self._last_notified_source_signature = file_signature
        if (source_changed or copied) and self._on_change is not None:
            try:
                self._on_change()
            except Exception as exc:
                print(f"[AuthSync] 同步回调异常: {exc}\n{traceback.format_exc()}", flush=True)

    def activate_auth_file(self, refresh_token: str) -> tuple[bool, str]:
        if not refresh_token:
            return False, "刷新令牌不能为空。"

        self.target_dir.mkdir(parents=True, exist_ok=True)
        target_path = self._find_auth_file_by_refresh_token(refresh_token)
        if target_path is None:
            return False, f"找不到刷新令牌对应的授权文件: {refresh_token}"

        try:
            data = self._read_auth_data(target_path)
            if data is None:
                return False, f"授权文件格式无效: {target_path}"
            self.source_path.parent.mkdir(parents=True, exist_ok=True)
            self._write_auth_data(self.source_path, self._strip_manager_metadata(data))
        except OSError as exc:
            return False, str(exc)

        with self._state_lock:
            self._source_signature = None
            self._source_state = None
            self._last_notified_source_signature = None

        if self._on_change is not None:
            try:
                self._on_change()
            except Exception as exc:
                print(f"[AuthSync] 激活回调异常: {exc}\n{traceback.format_exc()}", flush=True)
        return True, ""

    def delete_auth_file(self, refresh_token: str) -> tuple[bool, str]:
        if not refresh_token:
            return False, "刷新令牌不能为空。"

        self.target_dir.mkdir(parents=True, exist_ok=True)
        target_path = self._find_auth_file_by_refresh_token(refresh_token)
        if target_path is None:
            return False, f"找不到刷新令牌对应的授权文件: {refresh_token}"

        current_state = self._read_source_state(force=True)
        current_refresh_token = current_state["refresh_token"] if current_state is not None else ""

        try:
            if current_refresh_token == refresh_token and self.source_path.exists():
                self.source_path.unlink()
            target_path.unlink()
        except OSError as exc:
            return False, str(exc)

        with self._state_lock:
            self._quota_by_refresh_token.pop(refresh_token, None)
            self._plan_type_by_refresh_token.pop(refresh_token, None)
            self._user_id_by_refresh_token.pop(refresh_token, None)
            self._email_by_refresh_token.pop(refresh_token, None)
            self._quota_refresh_time_5h_by_refresh_token.pop(refresh_token, None)
            self._quota_refresh_time_7d_by_refresh_token.pop(refresh_token, None)
            self._traffic_by_refresh_token.pop(refresh_token, None)
            self._disabled_refresh_tokens.discard(refresh_token)
            self._access_token_to_refresh_token = {
                access_token: mapped_refresh_token
                for access_token, mapped_refresh_token in self._access_token_to_refresh_token.items()
                if mapped_refresh_token != refresh_token
            }
            if current_refresh_token == refresh_token:
                self._source_signature = None
                self._source_state = None
                self._last_notified_source_signature = None

        if self._on_change is not None:
            try:
                self._on_change()
            except Exception as exc:
                print(f"[AuthSync] 删除回调异常: {exc}\n{traceback.format_exc()}", flush=True)
        return True, ""

    def set_auth_load_strategy(self, refresh_token: str, load_strategy: str) -> tuple[bool, str]:
        if not refresh_token:
            return False, "刷新令牌不能为空。"

        strategy = load_strategy.strip()
        if strategy not in _LOAD_STRATEGIES:
            return False, f"负载策略无效: {load_strategy}"

        self.target_dir.mkdir(parents=True, exist_ok=True)
        target_path = self._find_auth_file_by_refresh_token(refresh_token)
        if target_path is None:
            return False, f"找不到刷新令牌对应的授权文件: {refresh_token}"

        data = self._read_auth_data(target_path)
        if data is None:
            return False, f"授权文件格式无效: {target_path}"

        metadata = dict(self._manager_metadata(data))
        metadata.pop("disabled", None)
        if strategy != _LOAD_STRATEGY_NORMAL:
            metadata["load_strategy"] = strategy
            data[_MANAGER_METADATA_KEY] = metadata
        else:
            metadata.pop("load_strategy", None)
            if metadata:
                data[_MANAGER_METADATA_KEY] = metadata
            else:
                data.pop(_MANAGER_METADATA_KEY, None)

        try:
            self._write_auth_data(target_path, data)
        except OSError as exc:
            return False, str(exc)

        with self._state_lock:
            if strategy == _LOAD_STRATEGY_DISABLED:
                self._disabled_refresh_tokens.add(refresh_token)
            else:
                self._disabled_refresh_tokens.discard(refresh_token)

        if self._on_change is not None:
            try:
                self._on_change()
            except Exception as exc:
                print(f"[AuthSync] 负载策略回调异常: {exc}\n{traceback.format_exc()}", flush=True)
        return True, ""

    def set_auth_disabled(self, refresh_token: str, disabled: bool) -> tuple[bool, str]:
        strategy = _LOAD_STRATEGY_DISABLED if disabled else _LOAD_STRATEGY_NORMAL
        return self.set_auth_load_strategy(refresh_token, strategy)

    def set_auth_note(self, refresh_token: str, note: str) -> tuple[bool, str]:
        if not refresh_token:
            return False, "刷新令牌不能为空。"

        self.target_dir.mkdir(parents=True, exist_ok=True)
        target_path = self._find_auth_file_by_refresh_token(refresh_token)
        if target_path is None:
            return False, f"找不到刷新令牌对应的授权文件: {refresh_token}"

        data = self._read_auth_data(target_path)
        if data is None:
            return False, f"授权文件格式无效: {target_path}"

        metadata = dict(self._manager_metadata(data))
        value = " ".join(note.split())
        if value:
            metadata["note"] = value
            data[_MANAGER_METADATA_KEY] = metadata
        else:
            metadata.pop("note", None)
            if metadata:
                data[_MANAGER_METADATA_KEY] = metadata
            else:
                data.pop(_MANAGER_METADATA_KEY, None)

        try:
            self._write_auth_data(target_path, data)
        except OSError as exc:
            return False, str(exc)

        if self._on_change is not None:
            try:
                self._on_change()
            except Exception as exc:
                print(f"[AuthSync] 备注回调异常: {exc}\n{traceback.format_exc()}", flush=True)
        return True, ""

    def list_auth_rows(self) -> list[AuthFileRow]:
        current_state = self._read_source_state()
        current_refresh_token = current_state["refresh_token"] if current_state is not None else ""

        rows: list[AuthFileRow] = []
        with self._state_lock:
            quota_by_refresh_token = dict(self._quota_by_refresh_token)
            plan_type_by_refresh_token = dict(self._plan_type_by_refresh_token)
            user_id_by_refresh_token = dict(self._user_id_by_refresh_token)
            email_by_refresh_token = dict(self._email_by_refresh_token)
            quota_refresh_time_5h_by_refresh_token = dict(self._quota_refresh_time_5h_by_refresh_token)
            quota_refresh_time_7d_by_refresh_token = dict(self._quota_refresh_time_7d_by_refresh_token)
            traffic_by_refresh_token = dict(self._traffic_by_refresh_token)
            disabled_refresh_tokens = set(self._disabled_refresh_tokens)
        if not self.target_dir.exists():
            return rows

        for path in sorted(self.target_dir.glob("*.json"), key=lambda item: item.name):
            data = self._read_auth_data(path)
            if data is None:
                continue

            tokens = data.get("tokens")
            if not isinstance(tokens, dict):
                continue

            refresh_token = str(tokens.get("refresh_token") or path.stem)
            access_token = str(tokens.get("access_token") or "")
            load_strategy = self._load_strategy(data)
            disabled = load_strategy == _LOAD_STRATEGY_DISABLED or refresh_token in disabled_refresh_tokens
            if disabled:
                load_strategy = _LOAD_STRATEGY_DISABLED
            with self._state_lock:
                self._access_token_to_refresh_token[access_token] = refresh_token
            rows.append(
                AuthFileRow(
                    account_id=str(data.get("account_id") or tokens.get("account_id") or ""),
                    refresh_token=refresh_token,
                    last_refresh=str(data.get("last_refresh") or ""),
                    user_id=user_id_by_refresh_token.get(refresh_token, ""),
                    email=email_by_refresh_token.get(refresh_token, ""),
                    quota=quota_by_refresh_token.get(refresh_token, ""),
                    plan_type=plan_type_by_refresh_token.get(refresh_token, ""),
                    quota_refresh_time_5h=quota_refresh_time_5h_by_refresh_token.get(refresh_token, ""),
                    quota_refresh_time_7d=quota_refresh_time_7d_by_refresh_token.get(refresh_token, ""),
                    traffic=traffic_by_refresh_token.get(refresh_token, 0),
                    current=refresh_token == current_refresh_token,
                    disabled=disabled,
                    load_strategy=load_strategy,
                    note=self._auth_note(data),
                    file_name=path.name,
                    access_token=access_token,
                )
            )

        return rows
