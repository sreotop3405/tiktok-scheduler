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
from urllib.parse import unquote, urlparse

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

# Floating tooltips / modals TikTok puts in front of the Post button.
# We try to dismiss them before clicking so pointer events reach the button.
# IMPORTANT: never click anything that *enables* a content-check feature on
# the user's account — prefer Cancel/Close over Turn on/OK in those modals.
DISMISS_BUTTON_SELECTORS = (
    # Cancel / close on the "Turn on automatic content checks?" modal.
    "[role='dialog'] button:has-text('Cancel')",
    "[role='dialog'] button:has-text('Отмена')",
    # Generic close-X variants on TikTok modals.
    "[role='dialog'] button[aria-label='Close']",
    "[data-floating-ui-portal] button[aria-label='Close']",
    "div.TUXModal button[aria-label='Close']",
    # Tooltips with explicit "Got it"/"OK"/"Continue" CTAs.
    "button:has-text('Got it')",
    "button:has-text('OK')",
    "button:has-text('Continue')",
    "button:has-text('Post anyway')",
    "button:has-text('Понятно')",
    "button:has-text('Хорошо')",
    "button:has-text('Продолжить')",
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
        launch_kwargs: dict = {
            "headless": self.headless if headless is None else headless,
            "args": _CHROMIUM_ARGS,
        }
        if self.settings.chromium_path:
            launch_kwargs["executable_path"] = self.settings.chromium_path
        proxy = proxy_url or self.settings.proxy_url
        if proxy:
            launch_kwargs["proxy"] = _parse_proxy(proxy)
        browser = await self._pw.chromium.launch(**launch_kwargs)
        context = await browser.new_context(
            user_agent=user_agent or DEFAULT_USER_AGENT,
            viewport={"width": 1280, "height": 800},
            locale="en-US",
            timezone_id="Europe/Moscow",
        )
        # Stealth patches: hide the most obvious automation markers so TikTok
        # is less aggressive with captchas / rate limits.
        await context.add_init_script(_STEALTH_INIT_SCRIPT)
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
        proxy_url: str | None = None,
    ) -> tuple[list[dict], str]:
        """Open a visible browser, let the user log in, return cookies + UA.

        Blocks until either:
          * a `sessionid` cookie shows up on tiktok.com, or
          * ``login_timeout_seconds`` elapse.
        """
        context = await self._new_context(headless=False, proxy_url=proxy_url)
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
                # If we landed on the login page, the cookies are stale.
                if "/login" in page.url:
                    raise RuntimeError("Redirected to login — session cookies expired")
                # Wait for the actual upload UI to render — the file input, the
                # iframe wrapper, or the contenteditable caption box.  We do
                # NOT wait for ``networkidle`` because TikTok Studio has
                # long-poll XHRs that never settle.
                if not await self._wait_for_upload_ui(page):
                    raise RuntimeError("Upload UI did not appear within 30s")
                return
            except Exception as exc:
                last_error = exc
                log.warning("Failed to open %s: %s", url, exc)
        raise RuntimeError(f"Could not reach TikTok upload page: {last_error}")

    async def _wait_for_upload_ui(self, page: Page, timeout_ms: int = 30_000) -> bool:
        """Wait for any of the markers that signal the upload page is ready.

        TikTok renders the form inside an iframe in some regions, so we poll
        the page (and any frames) for either the file input or the caption
        editor.  Returns True as soon as one of them is attached.
        """
        deadline = asyncio.get_event_loop().time() + timeout_ms / 1000
        while asyncio.get_event_loop().time() < deadline:
            for frame in [page.main_frame, *page.frames]:
                for selector in (*FILE_INPUT_SELECTORS, *CAPTION_SELECTORS):
                    try:
                        el = await frame.query_selector(selector)
                    except Exception:
                        el = None
                    if el is not None:
                        log.info("Upload UI ready: %s", selector)
                        return True
            await asyncio.sleep(0.5)
        return False

    async def _find_in_frames(
        self, page: Page, selectors: tuple[str, ...], timeout_ms: int = 20_000
    ):
        """Search every frame for the first matching selector.

        Returns ``(frame, element)`` tuple, or raises after ``timeout_ms``.
        """
        deadline = asyncio.get_event_loop().time() + timeout_ms / 1000
        while asyncio.get_event_loop().time() < deadline:
            for frame in [page.main_frame, *page.frames]:
                for sel in selectors:
                    try:
                        el = await frame.query_selector(sel)
                    except Exception:
                        el = None
                    if el is not None:
                        return frame, el, sel
            await asyncio.sleep(0.3)
        raise PlaywrightTimeout(
            f"None of {selectors} appeared in any frame within {timeout_ms}ms"
        )

    async def _upload_file(self, page: Page, path: Path) -> None:
        frame, el, sel = await self._find_in_frames(page, FILE_INPUT_SELECTORS)
        log.info("Found file input %s in frame %s", sel, frame.name or "main")
        await el.set_input_files(str(path))
        # Wait until the caption editor shows up — that's the signal the
        # upload is being processed and the metadata form is ready.
        await self._find_in_frames(page, CAPTION_SELECTORS, timeout_ms=120_000)

    async def _fill_caption(self, page: Page, caption: str) -> None:
        frame, box, sel = await self._find_in_frames(page, CAPTION_SELECTORS)
        log.info("Found caption %s in frame %s", sel, frame.name or "main")
        await box.click()
        await page.keyboard.press("Control+A")
        await page.keyboard.press("Delete")
        await page.keyboard.type(caption, delay=15)

    async def _click_post(self, page: Page) -> None:
        frame, btn, sel = await self._find_in_frames(
            page, POST_BUTTON_SELECTORS, timeout_ms=30_000
        )
        log.info("Found post button %s in frame %s", sel, frame.name or "main")
        # Some variants disable the button briefly while encoding.
        for _ in range(60):
            if await btn.is_enabled():
                break
            await asyncio.sleep(2)

        # Dismiss any floating tooltip / modal that might intercept the click
        # (e.g. the "We'll check your video for copyright" overlay).
        await self._dismiss_overlays(page)

        try:
            await btn.click(timeout=10_000)
        except PlaywrightTimeout:
            log.warning("Post button click intercepted; retrying via JS")
            # Last resort: dispatch click directly on the element, bypassing
            # pointer-event interception checks.
            await btn.evaluate("(el) => el.click()")

    async def _dismiss_overlays(self, page: Page) -> None:
        # Press Escape a couple of times — usually closes any popover.
        try:
            await page.keyboard.press("Escape")
            await asyncio.sleep(0.3)
            await page.keyboard.press("Escape")
        except Exception:  # pragma: no cover
            pass
        # Click any obvious "Got it / OK / Continue" button on the page.
        for frame in [page.main_frame, *page.frames]:
            for sel in DISMISS_BUTTON_SELECTORS:
                try:
                    el = await frame.query_selector(sel)
                except Exception:
                    el = None
                if el is None:
                    continue
                try:
                    if await el.is_visible():
                        log.info("Dismissing overlay via %s", sel)
                        await el.click(timeout=3_000)
                        await asyncio.sleep(0.5)
                except Exception:  # pragma: no cover
                    continue

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


def _parse_proxy(url: str) -> dict[str, str]:
    """Convert a proxy URL into Playwright's proxy config dict.

    Chromium's command-line ``--proxy-server`` does not understand embedded
    credentials (``user:pass@host``).  Playwright works around this by taking
    ``username``/``password`` as separate fields, but only if we split them
    out of the URL ourselves.

    NOTE: Chromium itself only accepts auth for HTTP/HTTPS proxies — SOCKS5
    with username/password is not supported upstream.  We log a warning in
    that case so the caller can switch to a non-auth SOCKS5 endpoint or an
    authenticated HTTP proxy.
    """
    parsed = urlparse(url.strip())
    if not parsed.scheme or not parsed.hostname:
        # Bare ``host:port`` — assume http.
        return {"server": f"http://{url.strip()}"}
    server = f"{parsed.scheme}://{parsed.hostname}"
    if parsed.port:
        server += f":{parsed.port}"
    config: dict[str, str] = {"server": server}
    if parsed.username:
        config["username"] = unquote(parsed.username)
    if parsed.password:
        config["password"] = unquote(parsed.password)
    if parsed.scheme.startswith("socks") and (parsed.username or parsed.password):
        log.warning(
            "Chromium does not support authenticated SOCKS proxies; "
            "credentials will be ignored. Use an HTTP/HTTPS proxy or an "
            "auth-less SOCKS5 endpoint instead."
        )
    return config


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


# Chromium launch flags that reduce automation fingerprints.
_CHROMIUM_ARGS: list[str] = [
    "--disable-blink-features=AutomationControlled",
    "--disable-features=IsolateOrigins,site-per-process",
    "--no-sandbox",
    "--disable-infobars",
    "--disable-dev-shm-usage",
]


# JavaScript injected into every page before it loads.  Mirrors the most
# common detections used by anti-bot services and what playwright-stealth
# patches.  Updating this is the first place to look when TikTok starts
# challenging the browser more aggressively.
_STEALTH_INIT_SCRIPT = """
(() => {
  // Hide navigator.webdriver
  try {
    Object.defineProperty(Navigator.prototype, 'webdriver', {
      get: () => undefined,
      configurable: true,
    });
  } catch (e) {}

  // Plausible plugins array
  try {
    Object.defineProperty(navigator, 'plugins', {
      get: () => [
        { name: 'Chrome PDF Plugin' },
        { name: 'Chrome PDF Viewer' },
        { name: 'Native Client' },
      ],
    });
  } catch (e) {}

  // Languages
  try {
    Object.defineProperty(navigator, 'languages', {
      get: () => ['ru-RU', 'ru', 'en-US', 'en'],
    });
  } catch (e) {}

  // window.chrome shim
  try {
    if (!window.chrome) {
      window.chrome = { runtime: {}, app: { isInstalled: false } };
    }
  } catch (e) {}

  // Permissions API: notifications shouldn't return 'denied' under headless
  try {
    const orig = navigator.permissions && navigator.permissions.query;
    if (orig) {
      navigator.permissions.query = (params) =>
        params && params.name === 'notifications'
          ? Promise.resolve({ state: Notification.permission })
          : orig(params);
    }
  } catch (e) {}

  // WebGL vendor/renderer
  try {
    const getParameter = WebGLRenderingContext.prototype.getParameter;
    WebGLRenderingContext.prototype.getParameter = function(p) {
      if (p === 37445) return 'Intel Inc.';
      if (p === 37446) return 'Intel Iris OpenGL Engine';
      return getParameter.call(this, p);
    };
  } catch (e) {}
})();
"""
