from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from app.models import RelayConfig


_PROVIDER_ID = "codex_session_relay"
_MANAGED_BEGIN = "# BEGIN CodexSessionManager relay"
_MANAGED_END = "# END CodexSessionManager relay"
_MODEL_PROVIDER_RE = re.compile(r"^\s*model_provider\s*=.*$")


@dataclass(frozen=True)
class RelayActivationResult:
    previous_model_provider_line: str = ""


class CodexLocalConfigService:
    def __init__(self, codex_home: Path | None = None) -> None:
        user_profile = os.environ.get("USERPROFILE") or str(Path.home())
        self.codex_home = codex_home or Path(user_profile) / ".codex"
        self.config_path = self.codex_home / "config.toml"
        self.auth_path = self.codex_home / "auth.json"

    def activate_relay(
        self,
        relay: RelayConfig,
        previous_model_provider_line: str = "",
        already_relay: bool = False,
    ) -> tuple[bool, str, RelayActivationResult | None]:
        try:
            original_config = self._read_text(self.config_path)
            original_auth = self.auth_path.read_bytes() if self.auth_path.exists() else None
            config_text, detected_previous = self._build_relay_config(original_config, relay)
            retained_previous = previous_model_provider_line if already_relay else detected_previous
            self._write_text(self.config_path, config_text)
            self._write_text(
                self.auth_path,
                json.dumps({"OPENAI_API_KEY": relay.api_key}, ensure_ascii=False, indent=2) + "\n",
            )
        except OSError as exc:
            self._restore_bytes(self.config_path, original_config.encode("utf-8") if 'original_config' in locals() else None)
            self._restore_bytes(self.auth_path, original_auth if 'original_auth' in locals() else None)
            return False, str(exc), None
        return True, "", RelayActivationResult(previous_model_provider_line=retained_previous)

    def restore_official_config(self, previous_model_provider_line: str) -> tuple[bool, str]:
        try:
            original = self._read_text(self.config_path)
            restored = self._remove_managed_block(original)
            lines = restored.splitlines()
            provider_index = self._find_top_level_provider_index(lines)
            if provider_index is not None:
                lines.pop(provider_index)
            if previous_model_provider_line:
                insert_at = self._first_table_index(lines)
                lines.insert(insert_at, previous_model_provider_line)
            restored = "\n".join(lines).rstrip() + ("\n" if lines else "")
            self._write_text(self.config_path, restored)
        except OSError as exc:
            return False, str(exc)
        return True, ""

    def _build_relay_config(self, original: str, relay: RelayConfig) -> tuple[str, str]:
        cleaned = self._remove_managed_block(original)
        lines = cleaned.splitlines()
        provider_index = self._find_top_level_provider_index(lines)
        previous_line = ""
        managed_provider_line = f'model_provider = "{_PROVIDER_ID}"'
        if provider_index is None:
            lines.insert(self._first_table_index(lines), managed_provider_line)
        else:
            previous_line = lines[provider_index]
            lines[provider_index] = managed_provider_line
        base = "\n".join(lines).rstrip()
        provider_block = "\n".join(
            [
                _MANAGED_BEGIN,
                f"[model_providers.{_PROVIDER_ID}]",
                f"name = {json.dumps(relay.name, ensure_ascii=False)}",
                f"base_url = {json.dumps(relay.base_url, ensure_ascii=False)}",
                'wire_api = "responses"',
                "requires_openai_auth = true",
                _MANAGED_END,
            ]
        )
        return f"{base}\n\n{provider_block}\n" if base else f"{managed_provider_line}\n\n{provider_block}\n", previous_line

    def _remove_managed_block(self, text: str) -> str:
        lines = text.splitlines()
        result: list[str] = []
        skipping = False
        for line in lines:
            if line.strip() == _MANAGED_BEGIN:
                skipping = True
                continue
            if skipping and line.strip() == _MANAGED_END:
                skipping = False
                continue
            if not skipping:
                result.append(line)
        return "\n".join(result).rstrip()

    def _find_top_level_provider_index(self, lines: list[str]) -> int | None:
        for index, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith("["):
                break
            if _MODEL_PROVIDER_RE.match(line):
                return index
        return None

    def _first_table_index(self, lines: list[str]) -> int:
        for index, line in enumerate(lines):
            if line.strip().startswith("["):
                return index
        return len(lines)

    def _read_text(self, path: Path) -> str:
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8-sig")

    def _write_text(self, path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f"{path.name}.codex-session-manager.tmp")
        temporary.write_text(text, encoding="utf-8", newline="\n")
        temporary.replace(path)

    def _restore_bytes(self, path: Path, data: bytes | None) -> None:
        try:
            if data is None:
                if path.exists():
                    path.unlink()
                return
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        except OSError:
            pass
