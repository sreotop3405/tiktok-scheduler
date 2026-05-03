"""Smoke tests for the SQLAlchemy storage layer using an in-memory SQLite."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tiktok_scheduler import storage
from tiktok_scheduler.models import Base


@pytest.fixture
def session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    async def _setup():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        return async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    factory = asyncio.get_event_loop().run_until_complete(_setup())
    try:
        yield factory
    finally:
        asyncio.get_event_loop().run_until_complete(engine.dispose())


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def test_create_account_and_schedule(session_factory):
    async def go():
        async with session_factory() as session:
            acc = await storage.create_account(session, name="alpha")
            await session.commit()
            assert acc.id is not None
            assert acc.status == "needs_login"
            await storage.upsert_schedule(
                session,
                acc.id,
                interval_minutes=120,
                start_hour=9,
                end_hour=22,
                caption="caption",
                hashtag="fyp",
            )
            await session.commit()
            sched = await storage.get_schedule_for_account(session, acc.id)
            assert sched is not None
            assert sched.interval_minutes == 120
            assert sched.hashtag == "fyp"

    _run(go())


def test_video_queue(session_factory, tmp_path: Path):
    async def go():
        async with session_factory() as session:
            acc = await storage.create_account(session, name="beta")
            await session.commit()
            v = tmp_path / "x.mp4"
            v.write_bytes(b"fake")
            await storage.add_video(
                session, acc.id, file_path=v, original_name="x.mp4"
            )
            await session.commit()
            assert await storage.queue_size(session, acc.id) == 1
            video = await storage.next_queued_video(session, acc.id)
            assert video is not None
            assert video.original_name == "x.mp4"

    _run(go())
