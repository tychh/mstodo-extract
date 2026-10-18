"""Graph's ISO timestamps and Windows/IANA dateTimeTimeZone values."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from tzlocal.windows_tz import win_tz

from .errors import ExportError


def parse_datetime(value: str, zone: str = "UTC") -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=ZoneInfo(win_tz.get(zone, zone)))
        return parsed
    except (ValueError, TypeError, ZoneInfoNotFoundError):
        raise ExportError("Invalid Microsoft To Do timestamp or unsupported time zone") from None


def milliseconds(value: str | dict[str, Any] | None) -> int | None:
    if not value:
        return None
    if isinstance(value, dict):
        date_time = value.get("dateTime")
        if not date_time:
            return None
        parsed = parse_datetime(date_time, value.get("timeZone") or "UTC")
    elif isinstance(value, str):
        parsed = parse_datetime(value)
    else:
        raise ExportError("Invalid Microsoft To Do timestamp shape")
    delta = parsed.astimezone(UTC) - datetime(1970, 1, 1, tzinfo=UTC)
    return (delta.days * 86400 + delta.seconds) * 1000 + delta.microseconds // 1000
