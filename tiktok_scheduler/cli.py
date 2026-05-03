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
) -> None:
    """Open a visible browser, sign into TikTok, save cookies for ``name``."""
    asyncio.run(_login(name=name, timeout=timeout))


async def _login(*, name: str, timeout: int) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    await init_db()
    async with session_scope() as session:
        account = await get_account_by_name(session, name)
        if account is None:
            account = await create_account(session, name=name)
            typer.echo(f"Created new account record for {name!r}")

    typer.echo("Launching browser. Log in to TikTok in the window that opens.")
    async with TikTokClient(headless=False) as client:
        cookies, ua = await client.interactive_login(login_timeout_seconds=timeout)

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
    """Import cookies exported from a browser extension (JSON list)."""
    asyncio.run(_import_cookies(name=name, path=path, user_agent=user_agent))


async def _import_cookies(*, name: str, path: Path, user_agent: str | None) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise typer.BadParameter("Cookie file must contain a JSON array")
    await init_db()
    async with session_scope() as session:
        account = await get_account_by_name(session, name)
        if account is None:
            account = await create_account(session, name=name)
        await save_session(session, account, data, user_agent)
    typer.echo(f"Imported {len(data)} cookies for {name!r}")


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
) -> None:
    """One-off post bypassing the scheduler — useful for smoke-testing."""
    asyncio.run(
        _post_now(name=name, file=file, caption=caption, hashtag=hashtag, headless=headless)
    )


async def _post_now(
    *, name: str, file: Path, caption: str, hashtag: str, headless: bool
) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    await init_db()
    async with session_scope() as session:
        account = await get_account_by_name(session, name)
        if account is None or not account.cookies_json:
            raise typer.BadParameter(f"No active session for {name!r}, run `login` first")
        cookies = json.loads(account.cookies_json)
        ua = account.user_agent
        proxy = account.proxy_url

    async with TikTokClient(headless=headless) as client:
        result = await client.post_video(
            video_path=file,
            caption=caption,
            hashtag=hashtag or None,
            cookies=cookies,
            user_agent=ua,
            proxy_url=proxy,
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


def _utcnow_str() -> str:  # pragma: no cover - debug
    return utcnow().isoformat()


if __name__ == "__main__":
    app()
