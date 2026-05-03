"""Timezone helpers."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from .config import get_settings


def local_tz() -> ZoneInfo:
    return ZoneInfo(get_settings().default_timezone)


def to_local(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(local_tz())


def to_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        # Treat naive as local time.
        dt = dt.replace(tzinfo=local_tz())
    return dt.astimezone(UTC)


def now_local() -> datetime:
    return datetime.now(tz=local_tz())


def fmt(dt: datetime | None) -> str:
    if dt is None:
        return "—"
    return to_local(dt).strftime("%Y-%m-%d %H:%M %Z")
