# crypto_dca

Long-only grid DCA trading bot for Bybit spot with pairwise loss compensation.

## Components

- `trader/` — async trading worker (long-running)
- `web/` — Django dashboard + config UI + REST API
- `tgbot/` — Telegram bot (notifications + control commands)
- `core/` — shared domain code (exchange client, strategy, models, services)

## Local setup

```bash
uv sync
cp .env.example .env  # fill in secrets
uv run pre-commit install
uv run python manage.py migrate
uv run python manage.py createsuperuser
```

## Run locally

```bash
uv run python manage.py runserver   # web (http://127.0.0.1:8000)
uv run python -m trader             # trading worker
uv run python -m tgbot              # telegram bot
```

## Checks

```bash
uv run ruff check
uv run ruff format --check
uv run mypy .
uv run pytest                       # unit + integration (integration skipped without keys)
uv run pytest --ignore=tests/integration  # unit only (what CI runs)
```

## Bootstrap a Telegram admin

```bash
uv run python manage.py add_tg_admin <chat_id> --label "Owner"
```

Send `/start` to the bot from that chat — only admins listed in `TelegramUser` can use commands.

## CI/CD

GitHub Actions (`.github/workflows/ci.yml`) runs on push/PR to `main`:
- `ruff check`, `ruff format --check`
- `mypy --strict`
- `pytest` (unit, integration excluded)

Integration tests against Bybit testnet are kept out of CI by default — run them locally
with `BYBIT_API_KEY=... BYBIT_API_SECRET=... BYBIT_TESTNET=1 uv run pytest -m integration tests/integration`.

## Railway deployment

Three services off the same repo, sharing a single Postgres + Redis plugin:

| Service | Start command | Config file |
|---------|---------------|-------------|
| `web` | `migrate && gunicorn web.wsgi:application --bind 0.0.0.0:$PORT` | `railway.json` (default) |
| `trader` | `python -m trader` | `railway.trader.json` |
| `tgbot` | `python -m tgbot` | `railway.tgbot.json` |

For each service in the Railway dashboard, set "Config Path" to the matching file. All three services share these env vars:

- `DATABASE_URL` — auto-injected from the Postgres plugin
- `REDIS_URL` — auto-injected from the Redis plugin
- `BYBIT_API_KEY`, `BYBIT_API_SECRET`, `BYBIT_TESTNET`
- `TELEGRAM_BOT_TOKEN`
- `DJANGO_SECRET_KEY`, `DJANGO_DEBUG=0`, `DJANGO_ALLOWED_HOSTS=<service-domain>`

First-run after deploy:
1. Open `web` service shell (or `railway run` locally with prod env): `python manage.py createsuperuser`
2. `python manage.py add_tg_admin <your_chat_id> --label "Owner"`
3. Log in at `<web-domain>/admin/`, set up `StrategyConfig` via the dashboard config UI
4. Ensure `BotStatus.paused = False`
5. Restart `trader` service to pick up config

Health check on `web`: `GET /healthz` (unauthenticated, no DB).

## Strategy

See `/home/grenkoff/.claude/plans/velvet-sprouting-lampson.md` for the full plan.
