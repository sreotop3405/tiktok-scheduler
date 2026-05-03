"""Tests for scheduler window/due logic.

These tests don't touch the database or Playwright — they verify the small
amount of logic that decides when to fire a post.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from tiktok_scheduler.models import Schedule
from tiktok_scheduler.scheduler import _is_due, _within_window

MSK = ZoneInfo("Europe/Moscow")


def _msk(year=2025, month=1, day=1, hour=12, minute=0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=MSK).astimezone(UTC)


def _schedule(**kwargs) -> Schedule:
    """Construct a Schedule without going through the DB.

    SQLAlchemy ``mapped_column(default=...)`` only applies on flush, so we set
    Python-side defaults explicitly here.
    """
    defaults: dict = {
        "account_id": 1,
        "interval_minutes": 180,
        "start_hour": 9,
        "end_hour": 23,
        "active": True,
        "caption": "",
        "hashtag": "",
        "next_post_at": None,
    }
    defaults.update(kwargs)
    return Schedule(**defaults)


def test_is_due_no_next_at():
    s = _schedule()
    assert _is_due(s, datetime.now(UTC)) is True


def test_is_due_inactive():
    s = _schedule(active=False)
    assert _is_due(s, datetime.now(UTC)) is False


def test_is_due_future():
    s = _schedule(next_post_at=datetime.now(UTC) + timedelta(hours=1))
    assert _is_due(s, datetime.now(UTC)) is False


def test_is_due_past():
    s = _schedule(next_post_at=datetime.now(UTC) - timedelta(hours=1))
    assert _is_due(s, datetime.now(UTC)) is True


def test_within_window_simple():
    s = _schedule(start_hour=9, end_hour=23)
    assert _within_window(s, _msk(hour=10)) is True
    assert _within_window(s, _msk(hour=23)) is False
    assert _within_window(s, _msk(hour=8, minute=59)) is False


def test_within_window_full_day():
    s = _schedule(start_hour=0, end_hour=0)
    assert _within_window(s, _msk(hour=3)) is True
    assert _within_window(s, _msk(hour=15)) is True


def test_within_window_overnight():
    s = _schedule(start_hour=22, end_hour=6)
    assert _within_window(s, _msk(hour=23)) is True
    assert _within_window(s, _msk(hour=2)) is True
    assert _within_window(s, _msk(hour=10)) is False
