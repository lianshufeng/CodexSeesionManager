from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib import error, request

from app.services.auth_sync_service import AuthSyncService


_REFRESH_TOKEN_URL = "https://auth.openai.com/oauth/token"
_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"


@dataclass
class AuthTokenRefreshResult:
    total: int = 0
    refreshed: int = 0
    skipped: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)


class AuthTokenRefreshService:
    def __init__(self, auth_sync_service: AuthSyncService) -> None:
        self.auth_sync_service = auth_sync_service

    def refresh_all(self, proxy_url: str = "") -> AuthTokenRefreshResult:
        result = AuthTokenRefreshResult()
        target_dir = self.auth_sync_service.target_dir
        if not target_dir.exists():
            return result

        opener = self._build_opener(proxy_url)
        seen_refresh_tokens: set[str] = set()
        for path in sorted(target_dir.glob("*.json"), key=lambda item: item.name):
            result.total += 1
            try:
                refreshed = self._refresh_path(path, opener, seen_refresh_tokens)
            except Exception as exc:
                result.failed += 1
                result.errors.append(f"{path.name}: {exc}")
                continue
            if refreshed:
                result.refreshed += 1
            else:
                result.skipped += 1

        if result.refreshed > 0:
            self.auth_sync_service.invalidate_cached_state()
        return result

    def _refresh_path(
        self,
        path: Path,
        opener: request.OpenerDirector,
        seen_refresh_tokens: set[str],
    ) -> bool:
        data = self._read_auth_data(path)
        tokens = data.get("tokens")
        if not isinstance(tokens, dict):
            return False

        old_refresh_token = str(tokens.get("refresh_token") or path.stem).strip()
        if not old_refresh_token or old_refresh_token in seen_refresh_tokens:
            return False
        seen_refresh_tokens.add(old_refresh_token)

        response = self._request_refresh(old_refresh_token, opener)
        new_access_token = str(response.get("access_token") or "").strip()
        if not new_access_token:
            raise RuntimeError("刷新接口未返回 access_token")

        new_id_token = str(response.get("id_token") or "").strip()
        new_refresh_token = str(response.get("refresh_token") or old_refresh_token).strip()
        tokens["access_token"] = new_access_token
        if new_id_token:
            tokens["id_token"] = new_id_token
        if new_refresh_token:
            tokens["refresh_token"] = new_refresh_token
        data["last_refresh"] = self._utc_now_text()

        self.auth_sync_service.save_refreshed_auth_file(path, data, old_refresh_token, new_refresh_token)
        return True

    def _request_refresh(self, refresh_token: str, opener: request.OpenerDirector) -> dict[str, object]:
        payload = json.dumps(
            {
                "client_id": _CLIENT_ID,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            },
            ensure_ascii=False,
        ).encode("utf-8")
        req = request.Request(
            _REFRESH_TOKEN_URL,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with opener.open(req, timeout=30) as resp:
                body = resp.read().decode("utf-8", errors="replace")
        except error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"HTTP {exc.code}: {self._extract_error_message(body)}") from exc
        except error.URLError as exc:
            raise RuntimeError(str(exc.reason)) from exc

        try:
            data = json.loads(body)
        except json.JSONDecodeError as exc:
            raise RuntimeError("刷新接口返回非 JSON") from exc
        if not isinstance(data, dict):
            raise RuntimeError("刷新接口返回格式无效")
        return data

    def _build_opener(self, proxy_url: str) -> request.OpenerDirector:
        proxy = proxy_url.strip()
        if not proxy:
            return request.build_opener()
        if "://" not in proxy:
            proxy = f"http://{proxy}"
        return request.build_opener(request.ProxyHandler({"http": proxy, "https": proxy}))

    def _read_auth_data(self, path: Path) -> dict[str, object]:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise RuntimeError("授权文件格式无效")
        return data

    def _write_auth_data(self, path: Path, data: dict[str, object]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def _extract_error_message(self, body: str) -> str:
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            return body.strip() or "请求失败"
        if isinstance(data, dict):
            error_value = data.get("error")
            if isinstance(error_value, dict):
                return str(error_value.get("message") or error_value.get("code") or error_value)
            if isinstance(error_value, str):
                return error_value
            return str(data.get("message") or data.get("code") or data)
        return str(data)

    def _utc_now_text(self) -> str:
        return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
