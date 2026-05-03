"""Minimal FastAPI dashboard.

Read-only for now: shows accounts, schedules, queue. The bot is the primary
control surface; the web UI is for at-a-glance status from a phone.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from ..config import get_settings
from ..db import session_scope
from ..storage import (
    get_schedule_for_account,
    list_accounts,
    list_videos,
)
from ..timezones import fmt

TEMPLATE_DIR = Path(__file__).parent / "templates"


def create_app() -> FastAPI:
    app = FastAPI(title="TikTok Scheduler", version="0.1.0")
    templates = Jinja2Templates(directory=str(TEMPLATE_DIR))

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> HTMLResponse:
        async with session_scope() as session:
            accounts = await list_accounts(session)
            data = []
            for acc in accounts:
                schedule = await get_schedule_for_account(session, acc.id)
                queued = await list_videos(
                    session, account_id=acc.id, statuses=("queued", "posting"), limit=20
                )
                posted = await list_videos(
                    session, account_id=acc.id, statuses=("posted",), limit=5
                )
                failed = await list_videos(
                    session, account_id=acc.id, statuses=("failed",), limit=5
                )
                data.append(
                    {
                        "account": acc,
                        "schedule": schedule,
                        "queued": queued,
                        "posted": posted,
                        "failed": failed,
                        "next_at": fmt(schedule.next_post_at) if schedule else "—",
                    }
                )
        return templates.TemplateResponse(
            request,
            "index.html",
            {"settings": get_settings(), "items": data, "fmt": fmt},
        )

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app
