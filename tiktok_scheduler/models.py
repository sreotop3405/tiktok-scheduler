"""SQLAlchemy ORM models."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class Account(Base):
    """A TikTok account whose session cookies we have stored."""

    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    cookies_json: Mapped[str | None] = mapped_column(Text, default=None)
    user_agent: Mapped[str | None] = mapped_column(Text, default=None)
    proxy_url: Mapped[str | None] = mapped_column(Text, default=None)
    status: Mapped[str] = mapped_column(String(32), default="needs_login")
    notes: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(default=None)

    schedules: Mapped[list[Schedule]] = relationship(
        back_populates="account", cascade="all, delete-orphan"
    )
    videos: Mapped[list[Video]] = relationship(
        back_populates="account", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:  # pragma: no cover - debug only
        return f"<Account #{self.id} {self.name!r} status={self.status}>"


class Schedule(Base):
    """Posting cadence configuration for an account."""

    __tablename__ = "schedules"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"))

    interval_minutes: Mapped[int] = mapped_column(default=180)
    # Posting window in the configured timezone (default Europe/Moscow).
    # Posts only fire when local hour is in [start_hour, end_hour).
    start_hour: Mapped[int] = mapped_column(default=9)
    end_hour: Mapped[int] = mapped_column(default=23)

    next_post_at: Mapped[datetime | None] = mapped_column(default=None)

    caption: Mapped[str] = mapped_column(Text, default="")
    hashtag: Mapped[str] = mapped_column(String(64), default="")

    active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)

    account: Mapped[Account] = relationship(back_populates="schedules")


class Video(Base):
    """A video file queued for upload to a specific account."""

    __tablename__ = "videos"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"))

    file_path: Mapped[str] = mapped_column(Text)
    original_name: Mapped[str] = mapped_column(Text)

    caption: Mapped[str | None] = mapped_column(Text, default=None)
    hashtag: Mapped[str | None] = mapped_column(String(64), default=None)

    status: Mapped[str] = mapped_column(String(16), default="queued")
    # statuses: queued, posting, posted, failed, skipped
    scheduled_at: Mapped[datetime | None] = mapped_column(default=None)
    posted_at: Mapped[datetime | None] = mapped_column(default=None)
    attempts: Mapped[int] = mapped_column(default=0)
    error: Mapped[str | None] = mapped_column(Text, default=None)

    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)

    account: Mapped[Account] = relationship(back_populates="videos")
