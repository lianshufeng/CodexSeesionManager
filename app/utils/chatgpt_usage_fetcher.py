from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from numbers import Number
import math
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener, urlopen

from app.models import QuotaWindow

_CHINA_TIMEZONE = timezone(timedelta(hours=8))


@dataclass
class UsageFetchResult:
    quota: str = ""
    plan_type: str = ""
    user_id: str = ""
    account_id: str = ""
    email: str = ""
    quota_refresh_time_5h: str = ""
    quota_refresh_time_7d: str = ""
    raw: dict[str, Any] | None = None
    message: str = ""

    @property
    def windows(self) -> tuple[QuotaWindow, ...]:
        rate_limit = (self.raw or {}).get("rate_limit")
        if not isinstance(rate_limit, dict):
            return ()
        windows = []
        for key in ("primary_window", "secondary_window"):
            window = rate_limit.get(key)
            if window is None:
                continue
            if not isinstance(window, dict):
                return ()
            values = [window.get(field) for field in ("used_percent", "reset_at", "limit_window_seconds")]
            if any(isinstance(value, bool) or not isinstance(value, Number) or not math.isfinite(value) for value in values):
                return ()
            used, reset_at, duration = map(float, values)
            if not 0 <= used <= 100 or reset_at <= 0 or duration <= 0:
                return ()
            windows.append(QuotaWindow(100 - used, reset_at, duration))
        return tuple(windows)


class ChatGPTUsageFetcher:
    def __init__(self) -> None:
        self.endpoint = "https://chatgpt.com/backend-api/wham/usage"

    def _log(self, message: str) -> None:
        print(f"[AuthUsage] {message}", flush=True)

    def _redact(self, value: str, keep_prefix: int = 8, keep_suffix: int = 4) -> str:
        if not value:
            return ""
        if len(value) <= keep_prefix + keep_suffix:
            return f"{value[:4]}..."
        return f"{value[:keep_prefix]}...{value[-keep_suffix:]}"

    def fetch(self, access_token: str, account_id: str = "", proxy_url: str | None = None) -> UsageFetchResult:
        if not access_token:
            return UsageFetchResult(message="access_token 为空")
        headers = {
            "user-agent": "codex-tui/0.121.0 (Windows 10.0.22631; x86_64) unknown (codex-tui; 0.121.0)",
            "authorization": f"Bearer {access_token}",
            "accept": "*/*",
            "host": "chatgpt.com",
        }
        if account_id:
            headers["chatgpt-account-id"] = account_id

        request = Request(self.endpoint, headers=headers)
        open_request = urlopen
        if proxy_url is not None:
            proxy_url = proxy_url.strip()
            if proxy_url and "://" not in proxy_url:
                proxy_url = "http://" + proxy_url
            open_request = build_opener(ProxyHandler({"http": proxy_url, "https": proxy_url} if proxy_url else {})).open
        try:
            with open_request(request, timeout=15) as response:
                body = response.read().decode("utf-8")
                payload = json.loads(body)
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
            self._log(f"请求失败: {exc}")
            return UsageFetchResult(message=str(exc))

        quota = self._summarize_quota(payload)
        if not quota:
            quota = "—"
        plan_type = self._summarize_plan_type(payload)
        user_id = self._summarize_string_field(payload, "user_id")
        parsed_account_id = self._summarize_string_field(payload, "account_id")
        email = self._summarize_string_field(payload, "email")
        quota_refresh_time_5h, quota_refresh_time_7d = self._summarize_quota_refresh_times(payload)
        raw = payload if isinstance(payload, dict) else None
        return UsageFetchResult(
            quota=quota,
            plan_type=plan_type,
            user_id=user_id,
            account_id=parsed_account_id,
            email=email,
            quota_refresh_time_5h=quota_refresh_time_5h,
            quota_refresh_time_7d=quota_refresh_time_7d,
            raw=raw,
        )

    def _summarize_quota(self, payload: Any) -> str:
        if isinstance(payload, dict):
            rate_limit = payload.get("rate_limit")
            if isinstance(rate_limit, dict):
                quota = self._summarize_rate_limit(rate_limit)
                if quota:
                    return quota

            percent = self._find_number(payload, {"percent", "percent_used", "usage_percent", "consumed_percent"})
            if percent is not None:
                return f"{self._format_number(percent)}%"

            used = self._find_number(payload, {"used", "usage", "total_usage", "current_usage", "amount_used"})
            limit = self._find_number(payload, {"limit", "hard_limit", "quota", "total", "monthly_limit"})
            if used is not None and limit is not None:
                return f"{self._format_number(used)} / {self._format_number(limit)}"

            remaining = self._find_number(payload, {"remaining", "available", "balance", "left"})
            if remaining is not None:
                return f"剩余 {self._format_number(remaining)}"

            for key in ("quota", "usage", "limit", "remaining", "available", "balance"):
                value = payload.get(key)
                if isinstance(value, (str, Number)):
                    return self._stringify_value(value)

            nested = payload.get("data") or payload.get("result") or payload.get("usage")
            if nested is not None:
                nested_quota = self._summarize_quota(nested)
                if nested_quota:
                    return nested_quota

            for value in payload.values():
                nested_quota = self._summarize_quota(value)
                if nested_quota:
                    return nested_quota

        if isinstance(payload, list):
            for item in payload:
                nested_quota = self._summarize_quota(item)
                if nested_quota:
                    return nested_quota

        if isinstance(payload, (str, Number)):
            return self._stringify_value(payload)

        return ""

    def _summarize_rate_limit(self, rate_limit: dict[str, Any]) -> str:
        primary_window = rate_limit.get("primary_window")
        secondary_window = rate_limit.get("secondary_window")

        primary_quota = self._window_remaining_percent(primary_window)
        secondary_quota = self._window_remaining_percent(secondary_window)

        if primary_quota and secondary_quota:
            return f"{primary_quota}/{secondary_quota}"
        if primary_quota:
            return primary_quota
        if secondary_quota:
            return secondary_quota
        return ""

    def _summarize_plan_type(self, payload: Any) -> str:
        if isinstance(payload, dict):
            value = payload.get("plan_type")
            if isinstance(value, str) and value.strip():
                return value.strip()
            nested = payload.get("data") or payload.get("result") or payload.get("usage")
            if nested is not None:
                nested_plan_type = self._summarize_plan_type(nested)
                if nested_plan_type:
                    return nested_plan_type
            for item in payload.values():
                nested_plan_type = self._summarize_plan_type(item)
                if nested_plan_type:
                    return nested_plan_type
        if isinstance(payload, list):
            for item in payload:
                nested_plan_type = self._summarize_plan_type(item)
                if nested_plan_type:
                    return nested_plan_type
        return ""

    def _summarize_string_field(self, payload: Any, field_name: str) -> str:
        if isinstance(payload, dict):
            value = payload.get(field_name)
            if isinstance(value, str) and value.strip():
                return value.strip()
            nested = payload.get("data") or payload.get("result") or payload.get("usage")
            if nested is not None:
                nested_value = self._summarize_string_field(nested, field_name)
                if nested_value:
                    return nested_value
            for item in payload.values():
                nested_value = self._summarize_string_field(item, field_name)
                if nested_value:
                    return nested_value
        if isinstance(payload, list):
            for item in payload:
                nested_value = self._summarize_string_field(item, field_name)
                if nested_value:
                    return nested_value
        return ""

    def _summarize_quota_refresh_times(self, payload: Any) -> tuple[str, str]:
        if isinstance(payload, dict):
            rate_limit = payload.get("rate_limit")
            if isinstance(rate_limit, dict):
                primary_reset_at = self._window_reset_at(rate_limit.get("primary_window"))
                secondary_reset_at = self._window_reset_at(rate_limit.get("secondary_window"))
                primary_time = self._format_epoch_seconds(primary_reset_at) if primary_reset_at is not None else ""
                secondary_time = (
                    self._format_epoch_seconds(secondary_reset_at) if secondary_reset_at is not None else ""
                )
                if primary_time or secondary_time:
                    return primary_time, secondary_time

            nested = payload.get("data") or payload.get("result") or payload.get("usage")
            if nested is not None:
                primary_time, secondary_time = self._summarize_quota_refresh_times(nested)
                if primary_time or secondary_time:
                    return primary_time, secondary_time
            for value in payload.values():
                primary_time, secondary_time = self._summarize_quota_refresh_times(value)
                if primary_time or secondary_time:
                    return primary_time, secondary_time

        if isinstance(payload, list):
            for item in payload:
                primary_time, secondary_time = self._summarize_quota_refresh_times(item)
                if primary_time or secondary_time:
                    return primary_time, secondary_time

        return "", ""

    def _window_remaining_percent(self, window: Any) -> str:
        if not isinstance(window, dict):
            return ""
        used_percent = window.get("used_percent")
        if not isinstance(used_percent, Number):
            return ""
        remaining = max(0.0, 100.0 - float(used_percent))
        return f"{self._format_number(remaining)}%"

    def _window_reset_at(self, window: Any) -> float | None:
        if not isinstance(window, dict):
            return None
        reset_at = window.get("reset_at")
        if isinstance(reset_at, Number):
            return float(reset_at)
        return None

    def _find_number(self, payload: dict[str, Any], wanted_keys: set[str]) -> float | None:
        for key, value in payload.items():
            key_name = key.lower()
            if key_name in wanted_keys and isinstance(value, Number):
                return float(value)
            if isinstance(value, dict):
                nested = self._find_number(value, wanted_keys)
                if nested is not None:
                    return nested
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, dict):
                        nested = self._find_number(item, wanted_keys)
                        if nested is not None:
                            return nested
        return None

    def _format_number(self, value: float) -> str:
        if value.is_integer():
            return str(int(value))
        return f"{value:.2f}".rstrip("0").rstrip(".")

    def _format_epoch_seconds(self, value: float) -> str:
        return (
            datetime.fromtimestamp(value, tz=timezone.utc)
            .astimezone(_CHINA_TIMEZONE)
            .strftime("%Y-%m-%d %H:%M:%S")
        )

    def _stringify_value(self, value: str | Number) -> str:
        if isinstance(value, Number):
            return self._format_number(float(value))
        text = str(value).strip()
        return text
