"""Configuration loaded from environment variables / .env file."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    telegram_bot_token: str = ""
    telegram_owner_ids: list[int] = Field(default_factory=list)

    data_dir: Path = Path("./data")

    web_host: str = "127.0.0.1"
    web_port: int = 8000
    web_enabled: bool = True

    default_timezone: str = "Europe/Moscow"

    headless_posting: bool = True
    chromium_path: str | None = None
    proxy_url: str | None = None

    max_post_attempts: int = 3
    retry_backoff_minutes: int = 15

    @field_validator("telegram_owner_ids", mode="before")
    @classmethod
    def _parse_owner_ids(cls, value: object) -> list[int]:
        if value is None or value == "":
            return []
        if isinstance(value, list):
            return [int(v) for v in value]
        if isinstance(value, str):
            return [int(part.strip()) for part in value.split(",") if part.strip()]
        raise ValueError("telegram_owner_ids must be a comma-separated list of integers")

    @property
    def db_path(self) -> Path:
        return self.data_dir / "tiktok_scheduler.db"

    @property
    def videos_dir(self) -> Path:
        return self.data_dir / "videos"

    @property
    def screenshots_dir(self) -> Path:
        return self.data_dir / "screenshots"

    @property
    def browser_profiles_dir(self) -> Path:
        return self.data_dir / "browser_profiles"

    def ensure_dirs(self) -> None:
        for path in (
            self.data_dir,
            self.videos_dir,
            self.screenshots_dir,
            self.browser_profiles_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_dirs()
    return settings
