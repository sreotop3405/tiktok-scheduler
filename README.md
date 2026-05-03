# tiktok-scheduler

Планировщик публикаций видео в TikTok с управлением через Telegram-бота и
простым веб-дашбордом. Работает по принципу «папка с видео + интервал»:
ты заливаешь ролики через бота, выбираешь аккаунт и cadence, дальше скрипт
сам выкладывает каждый следующий ролик через указанный интервал в заданное
окно по МСК.

> **Важно.** Автоматизация TikTok через эмуляцию браузера противоречит
> правилам платформы. Для своих 1–3 аккаунтов и аккуратного режима это
> обычно ок. Для масспостинга (десятки аккаунтов) одних только cookies
> недостаточно — нужны резидентные прокси, антидетект и антикапча.
> Этот проект — каркас, рассчитанный на расширение в эту сторону.

## Возможности

- Telegram-бот как основной интерфейс с телефона.
- Логин в TikTok через настоящий браузер один раз — далее cookies хранятся
  в SQLite, и публикации идут от твоего имени без капчи.
- Расписание на аккаунт: интервал в минутах + окно часов в МСК.
- Очередь видео: бот принимает Video / Document, складывает на диск,
  отдаёт планировщику.
- Простой веб-дашборд (FastAPI + Jinja) — статус и очередь.
- Скриншот в `data/screenshots/` при любой ошибке постинга.
- Уведомления в Telegram о каждом успешном/упавшем посте.
- CLI команды: `login`, `import-cookies`, `post-now`, `serve`,
  `list-accounts`.

## Стек

Python 3.11+, [aiogram 3](https://docs.aiogram.dev/),
[Playwright](https://playwright.dev/python/),
[APScheduler](https://apscheduler.readthedocs.io/) (через собственный
поллер), SQLAlchemy 2 + aiosqlite, FastAPI, Typer.

## Локальный запуск

### 1. Установка

```bash
git clone https://github.com/sreotop3405/tiktok-scheduler.git
cd tiktok-scheduler

# Рекомендую uv: https://github.com/astral-sh/uv
uv venv
source .venv/bin/activate
uv pip install -e .

# Браузеры для Playwright
python -m playwright install chromium
```

> На Linux дополнительно может понадобиться:
> `python -m playwright install-deps chromium`.

### 2. Конфигурация

```bash
cp .env.example .env
```

Открой `.env` и заполни:

- `TELEGRAM_BOT_TOKEN` — токен бота от
  [@BotFather](https://t.me/BotFather).
- `TELEGRAM_OWNER_IDS` — твой числовой Telegram ID, чтобы посторонние не
  могли использовать бота. Получить можно у
  [@userinfobot](https://t.me/userinfobot).

### 3. Создать БД

```bash
python -m tiktok_scheduler init-db
```

### 4. Залогинить первый аккаунт

```bash
python -m tiktok_scheduler login --name myacc
```

Откроется окно Chromium. Заходи в TikTok как обычно (через QR-код,
почту, телефон — что удобнее). Когда залогинился — cookies сохранятся
автоматически, окно можно закрыть.

> Если ты находишься на сервере без графики — пропусти этот шаг и
> используй `import-cookies` (см. ниже).

### 5. Запустить сервис

```bash
python -m tiktok_scheduler serve
```

Запускаются три компонента в одном процессе:

- Telegram-бот (long-polling)
- Воркер-планировщик
- Веб-UI на <http://127.0.0.1:8000>

Дальше в Telegram открой бота и шли:

```
/add               # создать второй аккаунт (если нужно)
/schedule          # выбрать аккаунт и задать расписание
/upload            # выбрать аккаунт и присылать видео файлами
/queue             # посмотреть, что в очереди
/pause myacc       # поставить аккаунт на паузу
```

## Импорт cookies из расширения браузера

Если нужно зайти в существующий аккаунт без перелогина — поставь
расширение [Cookie-Editor](https://cookie-editor.com/), на вкладке
TikTok нажми **Export → JSON**, сохрани в `cookies.json` и импортируй:

```bash
python -m tiktok_scheduler import-cookies \
    --name myacc \
    --file cookies.json \
    --user-agent "Mozilla/5.0 ... ваш UA"
```

User-Agent должен совпадать с тем, под которым ты выходил в TikTok,
иначе сессия может протухнуть.

## Тест публикации без планировщика

```bash
python -m tiktok_scheduler post-now \
    --name myacc \
    --file ./test.mp4 \
    --caption "тестовый пост" \
    --hashtag fyp \
    --headed
```

При `--headed` ты увидишь, что именно делает Playwright. Если что-то
сломается — найдёшь скриншот в `data/screenshots/`.

## Деплой на fly.io

В репо лежат `Dockerfile` и `fly.toml`. Минимальные шаги:

```bash
# Установи flyctl: https://fly.io/docs/hands-on/install-flyctl/
fly auth login
fly launch --no-deploy --name tiktok-scheduler
fly volumes create data --size 3 --region fra
fly secrets set TELEGRAM_BOT_TOKEN=... TELEGRAM_OWNER_IDS=...
fly deploy
```

Перед первым деплоем нужно залогинить аккаунт **локально**, остановить
сервис, скопировать `data/tiktok_scheduler.db` (или
прокинуть cookies через `import-cookies` уже на fly через
`fly ssh console`).

> На бесплатном тарфие fly.io 256 МБ RAM не хватит для Chromium —
> включи autostop и используй машину с 1 ГБ RAM (см. fly.toml).

## Ограничения и риски

- TikTok часто меняет селекторы — если что-то перестало работать, сначала
  посмотри скриншот в `data/screenshots/` и обнови селекторы в
  `tiktok_scheduler/tiktok/client.py`.
- Один IP + много аккаунтов = быстрый бан. Для масспостинга нужны
  резидентные прокси (поле `proxy_url` у `Account` в БД уже готово).
- Cookies живут ~30 дней; если посты начали падать с
  «Redirected to login» — перелогинься через `login`.
- Лимит Telegram-бота на размер видео — 50 МБ. Для больших видео
  используй `import` через локальную папку (см. ниже).

## Roadmap

- [ ] Поддержка приватности (public / friends / private).
- [ ] Поддержка cover-кадра (выбор обложки).
- [ ] Импорт видео из локальной папки / S3 (а не только через Telegram).
- [ ] Антидетект-профили (`browser_profiles_dir` уже зарезервирован).
- [ ] Веб-форма для смены caption/hashtag/расписания.
- [ ] Метрики постинга (uploads/day, success rate).

## Лицензия

MIT.
