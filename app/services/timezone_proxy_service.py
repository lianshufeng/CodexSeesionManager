from __future__ import annotations

import sys
import subprocess
from pathlib import Path

from app.services.proxy_service import ProxyConfig, ProxyService
from app.services.timezone_rewrite_service import validate_timezone
from app.services.language_rewrite_service import validate_language


class TimezoneProxyService(ProxyService):
    """Instance-only entry point; the shared proxy retains auth/traffic ownership."""

    def __init__(self, timezone_name: str | None, upstream_port: int, language_name: str | None = None) -> None:
        super().__init__(ProxyConfig(host="127.0.0.1", upstream_proxy=f"127.0.0.1:{upstream_port}"))
        self.timezone_name = validate_timezone(timezone_name) if timezone_name else ""
        self.language_name = validate_language(language_name) if language_name else ""
        self.config.port = self.find_available_port(upstream_port + 1)

    def _proxy_logger_script(self) -> Path:
        candidate = Path(sys.argv[0]).resolve().with_name("timezone_proxy_addon.py")
        return candidate if candidate.exists() else Path(__file__).with_name("timezone_proxy_addon.py")

    def _process_environment(self) -> dict[str, str]:
        env = super()._process_environment()
        env.pop("CODEX_SESSION_TIMEZONE", None)
        env.pop("CODEX_SESSION_LANGUAGE", None)
        if self.timezone_name:
            env["CODEX_SESSION_TIMEZONE"] = self.timezone_name
        if self.language_name:
            env["CODEX_SESSION_LANGUAGE"] = self.language_name
        return env

    def _extra_arguments(self) -> list[str]:
        return ["--set", f"ssl_verify_upstream_trusted_ca={self.certificate_files()['cer']}"]

    def _stop_matching_processes(self) -> None:
        # An instance proxy must never fall back to terminating other proxy processes.
        return

    def stop(self) -> None:
        process = self.process
        super().stop()
        if process is not None:
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                return
            if process.stdout is not None:
                process.stdout.close()
