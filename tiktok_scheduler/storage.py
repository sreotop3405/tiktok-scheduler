"""Data-access helpers used by the bot, scheduler, and web UI."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Account, Schedule, Video, utcnow

# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------


async def get_account_by_name(session: AsyncSession, name: str) -> Account | None:
    result = await session.execute(select(Account).where(Account.name == name))
    return result.scalar_one_or_none()


async def get_account(session: AsyncSession, account_id: int) -> Account | None:
    return await session.get(Account, account_id)


async def list_accounts(session: AsyncSession) -> list[Account]:
    result = await session.execute(select(Account).order_by(Account.id))
    return list(result.scalars())


async def create_account(
    session: AsyncSession,
    name: str,
    *,
    proxy_url: str | None = None,
    notes: str | None = None,
) -> Account:
    account = Account(name=name, proxy_url=proxy_url, notes=notes, status="needs_login")
    session.add(account)
    await session.flush()
    return account


async def save_session(
    session: AsyncSession,
    account: Account,
    cookies: list[dict],
    user_agent: str | None,
) -> None:
    account.cookies_json = json.dumps(cookies)
    account.user_agent = user_agent
    account.status = "active"
    account.last_used_at = utcnow()
    session.add(account)


def load_cookies(account: Account) -> list[dict]:
    if not account.cookies_json:
        return []
    try:
        return json.loads(account.cookies_json)
    except json.JSONDecodeError:
        return []


# ---------------------------------------------------------------------------
# Schedules
# ---------------------------------------------------------------------------


async def get_schedule_for_account(
    session: AsyncSession, account_id: int
) -> Schedule | None:
    result = await session.execute(
        select(Schedule).where(Schedule.account_id == account_id).order_by(Schedule.id)
    )
    return result.scalars().first()


async def upsert_schedule(
    session: AsyncSession,
    account_id: int,
    *,
    interval_minutes: int,
    start_hour: int,
    end_hour: int,
    caption: str,
    hashtag: str,
    active: bool = True,
) -> Schedule:
    schedule = await get_schedule_for_account(session, account_id)
    if schedule is None:
        schedule = Schedule(account_id=account_id)
        session.add(schedule)
    schedule.interval_minutes = interval_minutes
    schedule.start_hour = start_hour
    schedule.end_hour = end_hour
    schedule.caption = caption
    schedule.hashtag = hashtag.lstrip("#")
    schedule.active = active
    if schedule.next_post_at is None:
        schedule.next_post_at = datetime.now(UTC)
    await session.flush()
    return schedule


async def list_active_schedules(session: AsyncSession) -> list[Schedule]:
    result = await session.execute(select(Schedule).where(Schedule.active.is_(True)))
    return list(result.scalars())


# ---------------------------------------------------------------------------
# Videos
# ---------------------------------------------------------------------------


async def add_video(
    session: AsyncSession,
    account_id: int,
    *,
    file_path: Path,
    original_name: str,
    caption: str | None = None,
    hashtag: str | None = None,
) -> Video:
    video = Video(
        account_id=account_id,
        file_path=str(file_path),
        original_name=original_name,
        caption=caption,
        hashtag=hashtag,
    )
    session.add(video)
    await session.flush()
    return video


async def list_videos(
    session: AsyncSession,
    *,
    account_id: int | None = None,
    statuses: tuple[str, ...] | None = None,
    limit: int | None = None,
) -> list[Video]:
    stmt = select(Video).order_by(Video.id)
    if account_id is not None:
        stmt = stmt.where(Video.account_id == account_id)
    if statuses:
        stmt = stmt.where(Video.status.in_(statuses))
    if limit:
        stmt = stmt.limit(limit)
    result = await session.execute(stmt)
    return list(result.scalars())


async def next_queued_video(
    session: AsyncSession, account_id: int
) -> Video | None:
    stmt = (
        select(Video)
        .where(Video.account_id == account_id, Video.status == "queued")
        .order_by(Video.id)
        .limit(1)
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def queue_size(session: AsyncSession, account_id: int) -> int:
    stmt = select(Video).where(Video.account_id == account_id, Video.status == "queued")
    result = await session.execute(stmt)
    return len(list(result.scalars()))
