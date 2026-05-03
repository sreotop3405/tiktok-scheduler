"""Telegram handlers — the user-facing API of the scheduler."""

from __future__ import annotations

import logging
import re
from pathlib import Path

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile,
    Document,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    Video,
)

from ..config import get_settings
from ..db import session_scope
from ..models import Account
from ..storage import (
    add_video,
    create_account,
    get_account_by_name,
    get_schedule_for_account,
    list_accounts,
    list_videos,
    queue_size,
    upsert_schedule,
)
from ..timezones import fmt

log = logging.getLogger(__name__)
router = Router(name="scheduler")

NAME_RE = re.compile(r"^[a-zA-Z0-9_.\-]{1,64}$")

PLAY = "\u25b6\ufe0f"
PAUSE = "\u23f8\ufe0f"


# ---------------------------------------------------------------------------
# Auth middleware (simple owner-id check)
# ---------------------------------------------------------------------------


@router.message.middleware()
async def auth_middleware(handler, event: Message, data: dict):
    settings = get_settings()
    owners = settings.owner_ids
    if owners and event.from_user and event.from_user.id not in owners:
        await event.answer("\u26d4\ufe0f Этот бот приватный.")
        return
    return await handler(event, data)


# ---------------------------------------------------------------------------
# FSM state groups
# ---------------------------------------------------------------------------


class AddAccountStates(StatesGroup):
    waiting_for_name = State()


class ScheduleStates(StatesGroup):
    waiting_for_account = State()
    waiting_for_interval = State()
    waiting_for_window = State()
    waiting_for_caption = State()
    waiting_for_hashtag = State()


class UploadStates(StatesGroup):
    waiting_for_account = State()
    receiving_videos = State()


# ---------------------------------------------------------------------------
# /start, /help
# ---------------------------------------------------------------------------


HELP_TEXT = (
    "<b>TikTok Scheduler</b>\n"
    "Я планирую публикации видео в TikTok.\n\n"
    "<b>Команды:</b>\n"
    "/accounts — список аккаунтов\n"
    "/add — добавить новый аккаунт\n"
    "/login &lt;name&gt; — как залогиниться (выполняется на хосте)\n"
    "/schedule — задать расписание для аккаунта\n"
    "/upload — загрузить видео в очередь\n"
    "/queue — что сейчас в очереди\n"
    "/pause &lt;name&gt;, /resume &lt;name&gt; — пауза/возобновить\n"
    "/cancel — отменить текущий диалог\n"
)


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    await message.answer(HELP_TEXT)


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(HELP_TEXT)


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Окей, диалог сброшен.")


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------


@router.message(Command("accounts"))
async def cmd_accounts(message: Message) -> None:
    async with session_scope() as session:
        accounts = await list_accounts(session)
        if not accounts:
            await message.answer(
                "Аккаунтов пока нет. Используй /add чтобы добавить."
            )
            return
        lines = ["<b>Аккаунты:</b>"]
        for acc in accounts:
            schedule = await get_schedule_for_account(session, acc.id)
            qsize = await queue_size(session, acc.id)
            if schedule:
                marker = PLAY if schedule.active else PAUSE
                sched_info = (
                    f"каждые {schedule.interval_minutes} мин, "
                    f"{schedule.start_hour:02d}:00–{schedule.end_hour:02d}:00 {marker}"
                )
            else:
                sched_info = "нет расписания"
            lines.append(
                f"• <b>{acc.name}</b> — {acc.status} | очередь: {qsize} | {sched_info}"
            )
        await message.answer("\n".join(lines))


@router.message(Command("add"))
async def cmd_add(message: Message, state: FSMContext) -> None:
    await state.set_state(AddAccountStates.waiting_for_name)
    await message.answer(
        "Введи короткое имя аккаунта (буквы, цифры, _-.). "
        "Это просто метка, не обязательно совпадает с TikTok username."
    )


@router.message(AddAccountStates.waiting_for_name)
async def add_account_name(message: Message, state: FSMContext) -> None:
    name = (message.text or "").strip()
    if not NAME_RE.match(name):
        await message.answer("Имя не подходит. Только буквы/цифры/_-., 1–64 символа.")
        return
    async with session_scope() as session:
        existing = await get_account_by_name(session, name)
        if existing is not None:
            await message.answer("Уже есть аккаунт с таким именем. Попробуй другое.")
            return
        await create_account(session, name=name)
    await state.clear()
    await message.answer(
        f"Аккаунт <b>{name}</b> создан со статусом <i>needs_login</i>.\n\n"
        f"Теперь на хосте, где запущен сервис, выполни:\n"
        f"<code>python -m tiktok_scheduler login --name {name}</code>\n"
        f"Откроется окно браузера — войди в TikTok как обычно. "
        f"Cookies сохранятся, и аккаунт станет <i>active</i>."
    )


@router.message(Command("login"))
async def cmd_login(message: Message) -> None:
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Использование: /login &lt;name&gt;")
        return
    name = parts[1].strip()
    await message.answer(
        f"Чтобы залогиниться, на хосте сервиса выполни:\n"
        f"<code>python -m tiktok_scheduler login --name {name}</code>"
    )


# ---------------------------------------------------------------------------
# Schedule
# ---------------------------------------------------------------------------


def _accounts_keyboard(accounts: list[Account]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=acc.name, callback_data=f"acc:{acc.id}")]
        for acc in accounts
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows or [[]])


@router.message(Command("schedule"))
async def cmd_schedule(message: Message, state: FSMContext) -> None:
    async with session_scope() as session:
        accounts = await list_accounts(session)
    if not accounts:
        await message.answer("Сначала добавь аккаунт через /add.")
        return
    await state.set_state(ScheduleStates.waiting_for_account)
    await message.answer(
        "Выбери аккаунт для расписания:",
        reply_markup=_accounts_keyboard(accounts),
    )


@router.callback_query(ScheduleStates.waiting_for_account, F.data.startswith("acc:"))
async def schedule_pick_account(call, state: FSMContext) -> None:
    account_id = int(call.data.split(":", 1)[1])
    await state.update_data(account_id=account_id)
    await state.set_state(ScheduleStates.waiting_for_interval)
    await call.message.answer(
        "Интервал между постами в минутах (например, 180 — каждые 3 часа):"
    )
    await call.answer()


@router.message(ScheduleStates.waiting_for_interval)
async def schedule_interval(message: Message, state: FSMContext) -> None:
    try:
        interval = int((message.text or "").strip())
        if interval < 5:
            raise ValueError("too small")
    except ValueError:
        await message.answer("Нужно целое число минут, минимум 5.")
        return
    await state.update_data(interval_minutes=interval)
    await state.set_state(ScheduleStates.waiting_for_window)
    await message.answer(
        "Окно публикаций в часах МСК, формат <code>9-23</code> "
        "(значит постить только с 09:00 до 23:00). "
        "Если хочешь круглые сутки — введи <code>0-24</code>:"
    )


@router.message(ScheduleStates.waiting_for_window)
async def schedule_window(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip().replace(" ", "")
    m = re.match(r"^(\d{1,2})-(\d{1,2})$", text)
    if not m:
        await message.answer("Формат: <code>9-23</code>.")
        return
    start, end = int(m.group(1)), int(m.group(2))
    if not (0 <= start <= 24 and 0 <= end <= 24):
        await message.answer("Часы должны быть 0..24.")
        return
    await state.update_data(start_hour=start, end_hour=end)
    await state.set_state(ScheduleStates.waiting_for_caption)
    await message.answer(
        "Текст описания (caption) для всех видео.\n"
        "Пришли строку (можно пустую — тогда будет только хештег):"
    )


@router.message(ScheduleStates.waiting_for_caption)
async def schedule_caption(message: Message, state: FSMContext) -> None:
    await state.update_data(caption=(message.text or "").strip())
    await state.set_state(ScheduleStates.waiting_for_hashtag)
    await message.answer(
        "Один хештег (без #), например <code>fyp</code>. "
        "Пришли <code>-</code> чтобы оставить пустым:"
    )


@router.message(ScheduleStates.waiting_for_hashtag)
async def schedule_hashtag(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip().lstrip("#")
    hashtag = "" if raw == "-" else raw
    data = await state.get_data()
    async with session_scope() as session:
        schedule = await upsert_schedule(
            session,
            account_id=data["account_id"],
            interval_minutes=data["interval_minutes"],
            start_hour=data["start_hour"],
            end_hour=data["end_hour"],
            caption=data.get("caption", ""),
            hashtag=hashtag,
            active=True,
        )
    await state.clear()
    await message.answer(
        f"Готово. Расписание #{schedule.id}: каждые "
        f"{schedule.interval_minutes} мин, "
        f"{schedule.start_hour:02d}:00–{schedule.end_hour:02d}:00 МСК.\n"
        f"Caption: «{schedule.caption}», хештег: "
        f"{('#' + schedule.hashtag) if schedule.hashtag else '—'}.\n\n"
        f"Закидывай видео через /upload."
    )


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------


@router.message(Command("upload"))
async def cmd_upload(message: Message, state: FSMContext) -> None:
    async with session_scope() as session:
        accounts = await list_accounts(session)
    if not accounts:
        await message.answer("Нет аккаунтов. Сначала /add.")
        return
    await state.set_state(UploadStates.waiting_for_account)
    await message.answer(
        "Куда загружаем?",
        reply_markup=_accounts_keyboard(accounts),
    )


@router.callback_query(UploadStates.waiting_for_account, F.data.startswith("acc:"))
async def upload_pick_account(call, state: FSMContext) -> None:
    account_id = int(call.data.split(":", 1)[1])
    await state.update_data(account_id=account_id, count=0)
    await state.set_state(UploadStates.receiving_videos)
    await call.message.answer(
        "Жду видео. Шли как <b>файлы</b> (Document) или как Video.\n"
        "Когда закончишь — /done."
    )
    await call.answer()


@router.message(UploadStates.receiving_videos, Command("done"))
async def upload_done(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    count = data.get("count", 0)
    await state.clear()
    await message.answer(f"Принял в очередь {count} видео.")


@router.message(UploadStates.receiving_videos, F.video | F.document | F.video_note)
async def upload_receive(message: Message, state: FSMContext) -> None:
    settings = get_settings()
    data = await state.get_data()
    account_id = data.get("account_id")
    if account_id is None:
        await state.clear()
        await message.answer(
            "Сессия загрузки сбилась — забыл к какому аккаунту кладу. "
            "Сделай /upload и нажми кнопку с аккаунтом ещё раз."
        )
        return

    file_id, original_name = _extract_file(message)
    if file_id is None:
        await message.answer("Не вижу файла. Прикрепи видео.")
        return

    bot = message.bot
    try:
        file = await bot.get_file(file_id)
        suffix = Path(original_name).suffix or ".mp4"
        target = settings.videos_dir / f"acc{account_id}_{file.file_unique_id}{suffix}"
        settings.videos_dir.mkdir(parents=True, exist_ok=True)
        await bot.download_file(file.file_path, destination=str(target))
    except Exception as exc:
        log.exception("Failed to download video from Telegram")
        msg = str(exc)
        if "file is too big" in msg.lower():
            hint = (
                "Telegram Bot API не отдаёт файлы > 20 МБ. "
                "Сожми видео, или положи их в папку и запусти на ПК "
                "`python -m tiktok_scheduler enqueue-folder "
                "--name <acc> --folder <путь>` (см. README)."
            )
        else:
            hint = msg[:200]
        await message.answer(f"❌ Не смог скачать: {hint}")
        return

    async with session_scope() as session:
        await add_video(
            session,
            account_id,
            file_path=target,
            original_name=original_name,
        )

    count = data.get("count", 0) + 1
    await state.update_data(count=count)
    await message.answer(f"Принял #{count}: {original_name}")


def _extract_file(message: Message) -> tuple[str | None, str]:
    if message.video is not None:
        v: Video = message.video
        return v.file_id, v.file_name or f"video_{v.file_unique_id}.mp4"
    if message.document is not None:
        d: Document = message.document
        return d.file_id, d.file_name or f"document_{d.file_unique_id}"
    if message.video_note is not None:
        n = message.video_note
        return n.file_id, f"videonote_{n.file_unique_id}.mp4"
    return None, ""


# ---------------------------------------------------------------------------
# Queue
# ---------------------------------------------------------------------------


@router.message(Command("queue"))
async def cmd_queue(message: Message) -> None:
    async with session_scope() as session:
        accounts = await list_accounts(session)
        if not accounts:
            await message.answer("Аккаунтов нет.")
            return
        lines = ["<b>Очередь:</b>"]
        for acc in accounts:
            queued = await list_videos(
                session, account_id=acc.id, statuses=("queued", "posting"), limit=5
            )
            posted = await list_videos(
                session, account_id=acc.id, statuses=("posted",), limit=3
            )
            failed = await list_videos(
                session, account_id=acc.id, statuses=("failed",), limit=3
            )
            schedule = await get_schedule_for_account(session, acc.id)
            next_at = fmt(schedule.next_post_at) if schedule else "—"
            lines.append(
                f"\n<b>{acc.name}</b> ({acc.status}) — следующая попытка: {next_at}"
            )
            lines.append(
                f"  В очереди: {len(queued)} | опубликовано: {len(posted)} | ошибок: {len(failed)}"
            )
            for v in queued:
                lines.append(f"    • #{v.id} {v.original_name} [{v.status}]")
            for v in failed:
                err = (v.error or "").splitlines()[0][:80]
                lines.append(f"    \u274c #{v.id} {v.original_name}: {err}")
        await message.answer("\n".join(lines))


# ---------------------------------------------------------------------------
# Pause / resume
# ---------------------------------------------------------------------------


@router.message(Command("pause"))
async def cmd_pause(message: Message) -> None:
    await _toggle_schedule(message, active=False)


@router.message(Command("resume"))
async def cmd_resume(message: Message) -> None:
    await _toggle_schedule(message, active=True)


async def _toggle_schedule(message: Message, *, active: bool) -> None:
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Использование: /pause &lt;name&gt; или /resume &lt;name&gt;")
        return
    name = parts[1].strip()
    async with session_scope() as session:
        account = await get_account_by_name(session, name)
        if account is None:
            await message.answer("Аккаунт не найден.")
            return
        schedule = await get_schedule_for_account(session, account.id)
        if schedule is None:
            await message.answer("У этого аккаунта нет расписания.")
            return
        schedule.active = active
        session.add(schedule)
    label = f"{PLAY} запущено" if active else f"{PAUSE} на паузе"
    await message.answer(f"Расписание для @{name} {label}.")


# Helper exported for screenshots in failure notifications.
def screenshot_input(path: Path) -> BufferedInputFile:
    return BufferedInputFile(path.read_bytes(), filename=path.name)
