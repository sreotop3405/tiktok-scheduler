"""Command-line entrypoint."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

import typer

from .config import get_settings
from .db import init_db, session_scope
from .models import utcnow
from .service import run_service
from .storage import (
    add_video,
    create_account,
    get_account_by_name,
    list_accounts,
    save_session,
)
from .tiktok import TikTokClient

app = typer.Typer(help="TikTok Scheduler — CLI")


@app.command()
def serve() -> None:
    """Run the bot, scheduler and web UI in one process."""
    asyncio.run(run_service())


@app.command()
def login(
    name: str = typer.Option(..., "--name", "-n", help="Account name to log in"),
    timeout: int = typer.Option(600, help="Login timeout in seconds"),
    proxy: str | None = typer.Option(
        None,
        "--proxy",
        help="Proxy URL, e.g. socks5://user:pass@host:1080 or http://host:8080",
    ),
) -> None:
    """Open a visible browser, sign into TikTok, save cookies for ``name``."""
    asyncio.run(_login(name=name, timeout=timeout, proxy=proxy))


async def _login(*, name: str, timeout: int, proxy: str | None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    await init_db()
    async with session_scope() as session:
        account = await get_account_by_name(session, name)
        if account is None:
            account = await create_account(session, name=name, proxy_url=proxy)
            typer.echo(f"Created new account record for {name!r}")
        elif proxy and not account.proxy_url:
            account.proxy_url = proxy

    typer.echo("Launching browser. Log in to TikTok in the window that opens.")
    async with TikTokClient(headless=False) as client:
        cookies, ua = await client.interactive_login(
            login_timeout_seconds=timeout, proxy_url=proxy
        )

    async with session_scope() as session:
        account = await get_account_by_name(session, name)
        assert account is not None
        await save_session(session, account, cookies, ua)
    typer.echo(f"Saved {len(cookies)} cookies for {name!r}.")


@app.command(name="import-cookies")
def import_cookies(
    name: str = typer.Option(..., "--name", "-n"),
    path: Path = typer.Option(..., "--file", "-f", exists=True, readable=True),
    user_agent: str | None = typer.Option(None, "--user-agent"),
) -> None:
    """Import cookies exported from a browser extension.

    Auto-detects JSON (Cookie-Editor) and Netscape (curl/wget) formats.
    """
    asyncio.run(_import_cookies(name=name, path=path, user_agent=user_agent))


async def _import_cookies(*, name: str, path: Path, user_agent: str | None) -> None:
    raw = path.read_text(encoding="utf-8").strip()
    if not raw:
        raise typer.BadParameter("Cookie file is empty")

    cookies = _parse_cookie_file(raw)
    if not cookies:
        raise typer.BadParameter(
            "No cookies found in file. Make sure you exported from Cookie-Editor "
            "as JSON or Netscape format."
        )

    await init_db()
    async with session_scope() as session:
        account = await get_account_by_name(session, name)
        if account is None:
            account = await create_account(session, name=name)
        await save_session(session, account, cookies, user_agent)
    typer.echo(f"Imported {len(cookies)} cookies for {name!r}")


def _parse_cookie_file(raw: str) -> list[dict]:
    """Parse a cookie file (JSON or Netscape format)."""
    stripped = raw.lstrip()
    if stripped.startswith("[") or stripped.startswith("{"):
        # JSON.  Cookie-Editor sometimes wraps the array in an object.
        data = json.loads(stripped)
        if isinstance(data, dict):
            for key in ("cookies", "data", "items"):
                if isinstance(data.get(key), list):
                    return data[key]
            raise typer.BadParameter("JSON cookie file must contain an array")
        if isinstance(data, list):
            return data
        raise typer.BadParameter("JSON cookie file must contain an array")

    # Netscape HTTP Cookie File:
    #   domain  TAB  include_subdomains  TAB  path  TAB  secure  TAB  expires
    #   TAB  name  TAB  value
    cookies: list[dict] = []
    for line in raw.splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        domain, include_sub, cookie_path, secure, expires, c_name, value = parts[:7]
        cookie: dict = {
            "name": c_name,
            "value": value,
            "domain": domain,
            "path": cookie_path or "/",
            "secure": secure.upper() == "TRUE",
            "httpOnly": include_sub.upper() == "TRUE",
        }
        try:
            exp = int(expires)
            if exp > 0:
                cookie["expirationDate"] = exp
        except ValueError:
            pass
        cookies.append(cookie)
    return cookies


@app.command(name="list-accounts")
def list_accounts_cmd() -> None:
    """List accounts and their state."""
    asyncio.run(_list_accounts())


async def _list_accounts() -> None:
    await init_db()
    async with session_scope() as session:
        accounts = await list_accounts(session)
        if not accounts:
            typer.echo("(no accounts)")
            return
        for acc in accounts:
            typer.echo(
                f"#{acc.id:>3}  {acc.name:<20}  status={acc.status:<12}  "
                f"last_used={acc.last_used_at or '—'}"
            )


@app.command(name="post-now")
def post_now(
    name: str = typer.Option(..., "--name", "-n"),
    file: Path = typer.Option(..., "--file", "-f", exists=True, readable=True),
    caption: str = typer.Option("", "--caption", "-c"),
    hashtag: str = typer.Option("", "--hashtag", "-t"),
    headless: bool = typer.Option(False, "--headless/--headed"),
    proxy: str | None = typer.Option(
        None,
        "--proxy",
        help="Override the account's proxy URL for this run",
    ),
) -> None:
    """One-off post bypassing the scheduler — useful for smoke-testing."""
    asyncio.run(
        _post_now(
            name=name,
            file=file,
            caption=caption,
            hashtag=hashtag,
            headless=headless,
            proxy=proxy,
        )
    )


async def _post_now(
    *,
    name: str,
    file: Path,
    caption: str,
    hashtag: str,
    headless: bool,
    proxy: str | None,
) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    await init_db()
    async with session_scope() as session:
        account = await get_account_by_name(session, name)
        if account is None or not account.cookies_json:
            raise typer.BadParameter(f"No active session for {name!r}, run `login` first")
        cookies = json.loads(account.cookies_json)
        ua = account.user_agent
        effective_proxy = proxy or account.proxy_url

    async with TikTokClient(headless=headless) as client:
        result = await client.post_video(
            video_path=file,
            caption=caption,
            hashtag=hashtag or None,
            cookies=cookies,
            user_agent=ua,
            proxy_url=effective_proxy,
        )
    if result.success:
        typer.echo(f"OK posted at {result.posted_at}")
    else:
        typer.echo(f"FAILED: {result.error}", err=True)
        if result.screenshot_path:
            typer.echo(f"screenshot: {result.screenshot_path}", err=True)


@app.command(name="init-db")
def init_db_cmd() -> None:
    """Create database tables."""
    asyncio.run(init_db())
    typer.echo(f"Initialized DB at {get_settings().db_path}")


_VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".webm", ".mkv"}


@app.command(name="enqueue-folder")
def enqueue_folder_cmd(
    name: str = typer.Option(..., "--name", "-n", help="Account name"),
    folder: Path = typer.Option(
        ...,
        "--folder",
        "-d",
        exists=True,
        file_okay=False,
        dir_okay=True,
        readable=True,
        help="Folder with video files to enqueue",
    ),
    move: bool = typer.Option(
        False, "--move", help="Move source files into data/videos instead of copying"
    ),
) -> None:
    """Bulk-enqueue every video from a folder into the account's queue.

    Useful when videos are too large for the Telegram /upload command (>20 MB).
    """
    asyncio.run(_enqueue_folder(name=name, folder=folder, move=move))


async def _enqueue_folder(*, name: str, folder: Path, move: bool) -> None:
    import shutil

    settings = get_settings()
    settings.videos_dir.mkdir(parents=True, exist_ok=True)

    files = sorted(
        p for p in folder.iterdir()
        if p.is_file() and p.suffix.lower() in _VIDEO_EXTENSIONS
    )
    if not files:
        typer.echo("No video files found in folder", err=True)
        raise typer.Exit(code=1)

    await init_db()
    async with session_scope() as session:
        account = await get_account_by_name(session, name)
        if account is None:
            raise typer.BadParameter(f"No account named {name!r}")
        for src in files:
            target = settings.videos_dir / f"acc{account.id}_{src.name}"
            if target.exists():
                # Avoid clobbering — append a counter.
                base = target.stem
                suffix = target.suffix
                idx = 1
                while target.exists():
                    target = settings.videos_dir / f"{base}_{idx}{suffix}"
                    idx += 1
            if move:
                shutil.move(str(src), str(target))
            else:
                shutil.copy2(str(src), str(target))
            await add_video(
                session,
                account.id,
                file_path=target,
                original_name=src.name,
            )
            typer.echo(f"  + {src.name}")
    typer.echo(f"Enqueued {len(files)} videos into {name!r}")


def _utcnow_str() -> str:  # pragma: no cover - debug
    return utcnow().isoformat()


if __name__ == "__main__":
    app()
