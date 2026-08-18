from __future__ import annotations

import hashlib
import json
from pathlib import Path
from urllib.parse import urlsplit

from app.models import CREDENTIAL_TYPE_RELAY_API, RelayConfig
from app.utils.path_utils import app_root


_MANAGER_METADATA_KEY = "_codex_session_manager"


class RelayConfigService:
    def __init__(self, target_dir: Path | None = None) -> None:
        self.target_dir = target_dir or app_root() / "auth"

    def list_relays(self) -> list[RelayConfig]:
        if not self.target_dir.exists():
            return []
        rows: list[RelayConfig] = []
        for path in sorted(self.target_dir.glob("*.json"), key=lambda item: item.name):
            relay = self._read_relay(path)
            if relay is not None:
                rows.append(relay)
        return rows

    def get_relay(self, credential_id: str) -> RelayConfig | None:
        for relay in self.list_relays():
            if relay.credential_id == credential_id:
                return relay
        return None

    def save_relay(
        self,
        credential_id: str,
        name: str,
        base_url: str,
        api_key: str,
        model: str = "",
        note: str = "",
    ) -> tuple[bool, str, RelayConfig | None]:
        normalized_name = " ".join(name.split())
        normalized_url = self._normalize_base_url(base_url)
        normalized_key = api_key.strip()
        normalized_note = " ".join(note.split())
        if not normalized_name:
            return False, "中转站名称不能为空。", None
        if not normalized_url:
            return False, "中转地址必须是有效的 http:// 或 https:// 地址。", None
        existing = self.get_relay(credential_id) if credential_id else None
        if not normalized_key and existing is not None:
            normalized_key = existing.api_key
        if not normalized_key:
            return False, "API Key 不能为空。", None
        relay_id = credential_id.strip() or self._new_id(normalized_name, normalized_url)
        file_name = existing.file_name if existing is not None else f"{relay_id}.json"
        payload = {
            "id": relay_id,
            "name": normalized_name,
            "base_url": normalized_url,
            "api_key": normalized_key,
            "model": model.strip(),
            _MANAGER_METADATA_KEY: {
                "credential_type": CREDENTIAL_TYPE_RELAY_API,
                "note": normalized_note,
            },
        }
        try:
            self.target_dir.mkdir(parents=True, exist_ok=True)
            (self.target_dir / file_name).write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError as exc:
            return False, str(exc), None
        return True, "", self.get_relay(relay_id)

    def delete_relay(self, credential_id: str) -> tuple[bool, str]:
        relay = self.get_relay(credential_id)
        if relay is None:
            return False, "找不到对应的中转配置。"
        try:
            (self.target_dir / relay.file_name).unlink()
        except OSError as exc:
            return False, str(exc)
        return True, ""

    def _read_relay(self, path: Path) -> RelayConfig | None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict):
            return None
        metadata = data.get(_MANAGER_METADATA_KEY)
        if not isinstance(metadata, dict):
            return None
        if str(metadata.get("credential_type") or "") != CREDENTIAL_TYPE_RELAY_API:
            return None
        credential_id = str(data.get("id") or path.stem).strip()
        name = " ".join(str(data.get("name") or "").split())
        base_url = self._normalize_base_url(str(data.get("base_url") or ""))
        api_key = str(data.get("api_key") or "").strip()
        if not credential_id or not name or not base_url or not api_key:
            return None
        return RelayConfig(
            credential_id=credential_id,
            name=name,
            base_url=base_url,
            api_key=api_key,
            model=str(data.get("model") or "").strip(),
            note=" ".join(str(metadata.get("note") or "").split()),
            file_name=path.name,
        )

    def _normalize_base_url(self, base_url: str) -> str:
        value = base_url.strip().rstrip("/")
        try:
            parsed = urlsplit(value)
        except ValueError:
            return ""
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return ""
        if parsed.query or parsed.fragment:
            return ""
        return value

    def _new_id(self, name: str, base_url: str) -> str:
        digest = hashlib.sha256(f"{name}\n{base_url}".encode("utf-8")).hexdigest()[:16]
        candidate = f"relay_{digest}"
        if self.get_relay(candidate) is None:
            return candidate
        index = 2
        while self.get_relay(f"{candidate}_{index}") is not None:
            index += 1
        return f"{candidate}_{index}"
