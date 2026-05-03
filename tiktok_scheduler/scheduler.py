"""Background worker that decides when to post the next video.

We deliberately keep the implementation simple: every ``poll_seconds`` we wake
up, look at each active schedule, decide if it's time to publish another
video, and run the Playwright posting flow.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from .config import Settings, get_settings
from .db import session_scope
from .models import Account, Schedule, Video, utcnow
from .storage import (
    list_active_schedules,
    load_cookies,
    next_queued_video,
)
from .tiktok import PostResult, TikTokClient
from .timezones import to_local

log = logging.getLogger(__name__)


class PostingWorker:
    """Owns the posting loop and the optional notification callback."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        poll_seconds: int = 30,
        notify=None,
    ):
        self.settings = settings or get_settings()
        self.poll_seconds = poll_seconds
        self.notify = notify  # optional async callable: (message: str) -> None
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None

    def start(self) -> asyncio.Task:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="posting-worker")
        return self._task

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            await self._task

    async def _run(self) -> None:
        log.info("Posting worker started, polling every %ss", self.poll_seconds)
        while not self._stop.is_set():
            try:
                await self._tick()
            except Exception:
                log.exception("Worker tick failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.poll_seconds)
            except TimeoutError:
                continue
        log.info("Posting worker stopped")

    async def _tick(self) -> None:
        async with session_scope() as session:
            schedules = await list_active_schedules(session)
            now = datetime.now(UTC)
            due: list[tuple[Schedule, Account, Video]] = []
            for schedule in schedules:
                account = await session.get(Account, schedule.account_id)
                if account is None or account.status != "active":
                    continue
                if not _is_due(schedule, now):
                    continue
                if not _within_window(schedule, now):
                    continue
                video = await next_queued_video(session, account.id)
                if video is None:
                    continue
                due.append((schedule, account, video))

            for _schedule, _account, video in due:
                video.status = "posting"
                video.attempts += 1
                video.scheduled_at = video.scheduled_at or now
                session.add(video)
            # commit transition before doing slow Playwright work
        # Run posts sequentially (one browser at a time) to keep memory low.
        for schedule, account, video in due:
            await self._post(account, schedule, video)

    async def _post(self, account: Account, schedule: Schedule, video: Video) -> None:
        log.info(
            "Posting video #%s (%s) for account %s",
            video.id,
            video.original_name,
            account.name,
        )
        cookies = load_cookies(account)
        if not cookies:
            await self._mark_failed(video.id, "Account has no saved cookies")
            return

        async with TikTokClient(self.settings) as client:
            result: PostResult = await client.post_video(
                video_path=__import__("pathlib").Path(video.file_path),
                caption=video.caption or schedule.caption,
                hashtag=video.hashtag or schedule.hashtag,
                cookies=cookies,
                user_agent=account.user_agent,
                proxy_url=account.proxy_url,
            )

        async with session_scope() as session:
            db_video = await session.get(Video, video.id)
            db_schedule = await session.get(Schedule, schedule.id)
            db_account = await session.get(Account, account.id)
            if db_video is None or db_schedule is None or db_account is None:
                return
            if result.success:
                db_video.status = "posted"
                db_video.posted_at = result.posted_at
                db_video.error = None
                db_account.last_used_at = utcnow()
                db_schedule.next_post_at = utcnow() + timedelta(
                    minutes=db_schedule.interval_minutes
                )
                session.add_all([db_video, db_schedule, db_account])
                msg = (
                    f"\u2705 Posted #{db_video.id} ({db_video.original_name}) "
                    f"for @{db_account.name}"
                )
            else:
                if db_video.attempts >= self.settings.max_post_attempts:
                    db_video.status = "failed"
                else:
                    db_video.status = "queued"
                db_video.error = result.error or "unknown error"
                db_schedule.next_post_at = utcnow() + timedelta(
                    minutes=self.settings.retry_backoff_minutes
                )
                session.add_all([db_video, db_schedule])
                msg = (
                    f"\u274c Post failed for #{db_video.id} ({db_video.original_name}) "
                    f"on @{db_account.name}: {result.error}"
                )

        if self.notify is not None:
            try:
                await self.notify(msg)
            except Exception:
                log.exception("notify failed")

    async def _mark_failed(self, video_id: int, error: str) -> None:
        async with session_scope() as session:
            video = await session.get(Video, video_id)
            if video is None:
                return
            video.status = "failed"
            video.error = error
            session.add(video)


def _is_due(schedule: Schedule, now: datetime) -> bool:
    if not schedule.active:
        return False
    if schedule.next_post_at is None:
        return True
    next_at = schedule.next_post_at
    if next_at.tzinfo is None:
        next_at = next_at.replace(tzinfo=UTC)
    return next_at <= now


def _within_window(schedule: Schedule, now: datetime) -> bool:
    local = to_local(now)
    start = schedule.start_hour
    end = schedule.end_hour
    if start == end:
        return True
    if start < end:
        return start <= local.hour < end
    # Wraps midnight, e.g. 22..6.
    return local.hour >= start or local.hour < end
