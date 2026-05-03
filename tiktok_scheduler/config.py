"""Configuration loaded from environment variables / .env file."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    telegram_bot_token: str = ""
    # Stored as raw string to avoid pydantic-settings trying to JSON-parse
    # the env var (which trips up on bare integers and CSV strings).
    # Use ``owner_ids`` to read it as a list of ints.
    telegram_owner_ids: str = ""
    # Optional proxy for outgoing connections to api.telegram.org.
    # Useful in regions where Telegram is throttled / blocked at the ISP.
    # Supports http://, https://, socks4://, socks5://.  Credentials may
    # be embedded: socks5://user:pass@host:1080
    telegram_proxy_url: str | None = None

    data_dir: Path = Path("./data")

    web_host: str = "127.0.0.1"
    # Avoid 8000 — on Windows the default Hyper-V / WSL excluded port ranges
    # often eat 8000-8009 and bind() fails with WinError 10013.
    web_port: int = 8765
    web_enabled: bool = True

    default_timezone: str = "Europe/Moscow"

    headless_posting: bool = True
    chromium_path: str | None = None
    proxy_url: str | None = None

    max_post_attempts: int = 3
    retry_backoff_minutes: int = 15

    @field_validator("telegram_owner_ids", mode="before")
    @classmethod
    def _coerce_owner_ids(cls, value: object) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        if isinstance(value, int):
            return str(value)
        if isinstance(value, (list, tuple)):
            return ",".join(str(v).strip() for v in value if str(v).strip())
        return str(value)

    @property
    def owner_ids(self) -> list[int]:
        result: list[int] = []
        for part in self.telegram_owner_ids.split(","):
            part = part.strip()
            if not part:
                continue
            try:
                result.append(int(part))
            except ValueError:
                continue
        return result

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
