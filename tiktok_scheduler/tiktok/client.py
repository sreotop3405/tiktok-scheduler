"""Playwright wrapper around the TikTok web uploader.

The selectors here are best-effort: TikTok regularly changes its DOM. The
methods are intentionally defensive — they try several selector strategies and
save a screenshot to ``data/screenshots`` on failure so you can update the
selectors without re-running the whole flow.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from playwright.async_api import (
    BrowserContext,
    Page,
    Playwright,
    async_playwright,
)
from playwright.async_api import (
    TimeoutError as PlaywrightTimeout,
)

from ..config import Settings, get_settings

log = logging.getLogger(__name__)

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

UPLOAD_URLS = (
    "https://www.tiktok.com/tiktokstudio/upload?from=upload",
    "https://www.tiktok.com/upload?lang=en",
)

# Possible selectors for the file input that accepts the video file.
FILE_INPUT_SELECTORS = (
    "input[type='file'][accept*='video']",
    "input[type='file']",
)

# Possible selectors for the caption editor (it is a contenteditable div).
CAPTION_SELECTORS = (
    "div[contenteditable='true'][data-text='true']",
    "div[contenteditable='true'][role='combobox']",
    "div[contenteditable='true']",
)

# Possible selectors for the Post / Publish button.
POST_BUTTON_SELECTORS = (
    "button[data-e2e='post_video_button']",
    "button:has-text('Post')",
    "button:has-text('Опубликовать')",
    "button:has-text('Publier')",
)

# After a successful post, TikTok usually navigates somewhere with these markers.
SUCCESS_MARKERS = (
    "text=Your video has been uploaded",
    "text=Your video is being uploaded",
    "text=Видео загружено",
    "[data-e2e='upload-success-modal']",
)


@dataclass
class PostResult:
    success: bool
    error: str | None = None
    screenshot_path: Path | None = None
    posted_at: datetime | None = None


class TikTokClient:
    """High level client for posting/login flows.

    Use as an async context manager:

        async with TikTokClient(settings) as client:
            await client.post_video(...)
    """

    def __init__(self, settings: Settings | None = None, *, headless: bool | None = None):
        self.settings = settings or get_settings()
        self.headless = self.settings.headless_posting if headless is None else headless
        self._pw: Playwright | None = None
        self._context: BrowserContext | None = None

    async def __aenter__(self) -> TikTokClient:
        self._pw = await async_playwright().start()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._context is not None:
            try:
                await self._context.close()
            except Exception:  # pragma: no cover - cleanup
                log.exception("Failed to close Playwright context")
        if self._pw is not None:
            await self._pw.stop()

    # ------------------------------------------------------------------
    # Browser management
    # ------------------------------------------------------------------

    async def _new_context(
        self,
        *,
        cookies: list[dict] | None = None,
        user_agent: str | None = None,
        proxy_url: str | None = None,
        headless: bool | None = None,
    ) -> BrowserContext:
        assert self._pw is not None
        launch_kwargs: dict = {"headless": self.headless if headless is None else headless}
        if self.settings.chromium_path:
            launch_kwargs["executable_path"] = self.settings.chromium_path
        proxy = proxy_url or self.settings.proxy_url
        if proxy:
            launch_kwargs["proxy"] = {"server": proxy}
        browser = await self._pw.chromium.launch(**launch_kwargs)
        context = await browser.new_context(
            user_agent=user_agent or DEFAULT_USER_AGENT,
            viewport={"width": 1280, "height": 800},
            locale="en-US",
        )
        if cookies:
            try:
                await context.add_cookies(_normalize_cookies(cookies))
            except Exception as exc:  # pragma: no cover
                log.warning("Failed to add cookies: %s", exc)
        self._context = context
        return context

    # ------------------------------------------------------------------
    # Public flows
    # ------------------------------------------------------------------

    async def interactive_login(
        self,
        *,
        login_timeout_seconds: int = 600,
    ) -> tuple[list[dict], str]:
        """Open a visible browser, let the user log in, return cookies + UA.

        Blocks until either:
          * a `sessionid` cookie shows up on tiktok.com, or
          * ``login_timeout_seconds`` elapse.
        """
        context = await self._new_context(headless=False)
        page = await context.new_page()
        await page.goto("https://www.tiktok.com/login", wait_until="domcontentloaded")

        log.info("Waiting for user to complete TikTok login (up to %ss)...", login_timeout_seconds)

        deadline = asyncio.get_event_loop().time() + login_timeout_seconds
        while asyncio.get_event_loop().time() < deadline:
            cookies = await context.cookies("https://www.tiktok.com")
            if any(c.get("name") == "sessionid" and c.get("value") for c in cookies):
                user_agent = await page.evaluate("() => navigator.userAgent")
                log.info("Login detected, saving %d cookies", len(cookies))
                return cookies, user_agent
            await asyncio.sleep(2)

        raise TimeoutError("Login was not completed in time")

    async def post_video(
        self,
        *,
        video_path: Path,
        caption: str,
        hashtag: str | None,
        cookies: list[dict],
        user_agent: str | None,
        proxy_url: str | None = None,
        wait_after_post_seconds: int = 30,
    ) -> PostResult:
        if not video_path.exists():
            return PostResult(success=False, error=f"video not found: {video_path}")

        context = await self._new_context(
            cookies=cookies,
            user_agent=user_agent,
            proxy_url=proxy_url,
        )
        page = await context.new_page()

        screenshots_dir = self.settings.screenshots_dir
        screenshots_dir.mkdir(parents=True, exist_ok=True)

        try:
            await self._navigate_to_upload(page)
            await self._upload_file(page, video_path)
            full_caption = _build_caption(caption, hashtag)
            if full_caption:
                await self._fill_caption(page, full_caption)
            await self._click_post(page)
            await self._wait_for_success(page, wait_after_post_seconds)
            return PostResult(success=True, posted_at=datetime.now(UTC))
        except Exception as exc:
            shot = screenshots_dir / f"fail_{int(datetime.now().timestamp())}.png"
            try:
                await page.screenshot(path=str(shot), full_page=True)
            except Exception:  # pragma: no cover
                shot = None
            log.exception("Posting failed")
            return PostResult(success=False, error=str(exc), screenshot_path=shot)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _navigate_to_upload(self, page: Page) -> None:
        last_error: Exception | None = None
        for url in UPLOAD_URLS:
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
                # Wait briefly for the upload UI to render.
                await page.wait_for_load_state("networkidle", timeout=15_000)
                # If we landed on the login page, the cookies are stale.
                if "/login" in page.url:
                    raise RuntimeError("Redirected to login — session cookies expired")
                return
            except Exception as exc:
                last_error = exc
                log.warning("Failed to open %s: %s", url, exc)
        raise RuntimeError(f"Could not reach TikTok upload page: {last_error}")

    async def _upload_file(self, page: Page, path: Path) -> None:
        for selector in FILE_INPUT_SELECTORS:
            try:
                el = await page.wait_for_selector(selector, state="attached", timeout=20_000)
                if el is None:
                    continue
                await el.set_input_files(str(path))
                # Wait for the editor (caption box) to appear, signalling upload is in progress.
                for cap in CAPTION_SELECTORS:
                    try:
                        await page.wait_for_selector(cap, state="visible", timeout=60_000)
                        return
                    except PlaywrightTimeout:
                        continue
                return
            except PlaywrightTimeout:
                continue
        raise RuntimeError("No file input found on the upload page")

    async def _fill_caption(self, page: Page, caption: str) -> None:
        for selector in CAPTION_SELECTORS:
            try:
                box = await page.wait_for_selector(selector, state="visible", timeout=10_000)
                if box is None:
                    continue
                await box.click()
                # Clear existing content.
                await page.keyboard.press("Control+A")
                await page.keyboard.press("Delete")
                await page.keyboard.type(caption, delay=15)
                return
            except PlaywrightTimeout:
                continue
        raise RuntimeError("Caption editor not found")

    async def _click_post(self, page: Page) -> None:
        for selector in POST_BUTTON_SELECTORS:
            try:
                btn = await page.wait_for_selector(selector, state="visible", timeout=15_000)
                if btn is None:
                    continue
                # Some variants disable the button briefly while encoding.
                for _ in range(60):
                    if await btn.is_enabled():
                        break
                    await asyncio.sleep(2)
                await btn.click()
                return
            except PlaywrightTimeout:
                continue
        raise RuntimeError("Post button not found")

    async def _wait_for_success(self, page: Page, seconds: int) -> None:
        deadline = asyncio.get_event_loop().time() + seconds
        last_error: Exception | None = None
        while asyncio.get_event_loop().time() < deadline:
            for marker in SUCCESS_MARKERS:
                try:
                    el = await page.query_selector(marker)
                    if el is not None:
                        return
                except Exception as exc:  # pragma: no cover
                    last_error = exc
            # Some flows redirect away from /upload after success.
            if "/upload" not in page.url and "/login" not in page.url:
                return
            await asyncio.sleep(2)
        if last_error:
            log.debug("Last success-detection error: %s", last_error)
        # We were not able to confirm success, but the post may have gone through.
        # Treat as soft success — caller can re-check later.
        log.warning("Could not positively confirm upload success; treating as posted")


def _normalize_cookies(cookies: Iterable[dict]) -> list[dict]:
    """Make raw cookies palatable to Playwright."""
    cleaned: list[dict] = []
    for c in cookies:
        cookie = {k: v for k, v in c.items() if v is not None}
        if "expires" in cookie and isinstance(cookie["expires"], float):
            cookie["expires"] = int(cookie["expires"])
        # Playwright requires either ``url`` or ``domain``+``path``.
        if "domain" not in cookie and "url" not in cookie:
            cookie["url"] = "https://www.tiktok.com"
        if "path" not in cookie:
            cookie["path"] = "/"
        if "sameSite" in cookie and cookie["sameSite"] not in ("Strict", "Lax", "None"):
            cookie["sameSite"] = "Lax"
        cleaned.append(cookie)
    return cleaned


def _build_caption(caption: str, hashtag: str | None) -> str:
    parts = [caption.strip()] if caption else []
    if hashtag:
        tag = hashtag.strip().lstrip("#")
        if tag:
            parts.append(f"#{tag}")
    return " ".join(parts).strip()
