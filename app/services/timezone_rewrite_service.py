from __future__ import annotations

import re
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo


DEFAULT_TIMEZONE = "Asia/Tokyo"
COMMON_TIMEZONES = (
    "Asia/Tokyo", "Asia/Shanghai", "Asia/Hong_Kong", "Asia/Singapore",
    "Asia/Seoul", "Asia/Kolkata", "Asia/Dubai", "UTC", "Europe/London",
    "Europe/Paris", "Europe/Berlin", "America/New_York", "America/Chicago",
    "America/Denver", "America/Los_Angeles", "Australia/Sydney", "Pacific/Auckland",
)
_CONTEXT = re.compile(
    r"\A\s*<(?P<tag>environment_context|codex_apps_client_time_context)>"
    r"(?P<body>.*?)</(?P=tag)>\s*\Z", re.DOTALL,
)
_ZONE = re.compile(r"(<timezone>)([^<>]*)(</timezone>)")
_DATE = re.compile(r"(<current_date>)(\d{4}-\d{2}-\d{2})(</current_date>)")


def validate_timezone(value: str) -> str:
    value = value.strip()
    ZoneInfo(value)
    return value


class TimezoneRewriter:
    """Rewrite generated context blocks, never arbitrary strings in a request."""

    def __init__(self, timezone_name: str) -> None:
        self.timezone_name = validate_timezone(timezone_name)
        self.zone = ZoneInfo(self.timezone_name)
        self.source_zone: ZoneInfo | None = None

    def rewrite(self, payload: object, now: datetime | None = None) -> bool:
        if not isinstance(payload, dict) or not isinstance(payload.get("input"), list):
            return False
        now = now or datetime.now(timezone.utc)
        # A diff may contain only a date. The latest preceding context supplies its zone.
        source_zone = self.source_zone
        changed = False
        for item in payload["input"]:
            if not isinstance(item, dict) or item.get("role") not in ("user", "developer"):
                continue
            content = item.get("content")
            if not isinstance(content, list):
                continue
            for part in content:
                if not isinstance(part, dict) or part.get("type") != "input_text":
                    continue
                original = part.get("text")
                if not isinstance(original, str) or not _CONTEXT.fullmatch(original):
                    continue
                match = _ZONE.search(original)
                if match:
                    try:
                        source_zone = ZoneInfo(match[2].strip())
                    except (ValueError, KeyError):
                        source_zone = None
                updated = _ZONE.sub(lambda m: m[1] + self.timezone_name + m[3], original)
                if source_zone is not None:
                    # Preserve historical dates instead of replacing all history with today.
                    delta = now.astimezone(self.zone).date() - now.astimezone(source_zone).date()
                    def shift_date(m: re.Match[str]) -> str:
                        try:
                            return m[1] + (date.fromisoformat(m[2]) + delta).isoformat() + m[3]
                        except ValueError:
                            return m[0]
                    updated = _DATE.sub(shift_date, updated)
                if updated != original:
                    part["text"] = updated
                    changed = True
        self.source_zone = source_zone
        return changed
