# crypto_dca

Long-only grid DCA trading bot for Bybit spot with pairwise loss compensation.

## Components

- `trader/` — async trading worker (long-running)
- `tgbot/` — Telegram bot (notifications + control commands)
- `cli/` — operator CLIs (preflight, consolidate, add-admin)
- `core/` — shared domain code (exchange client, strategy, DAO, services)

## Local setup

```bash
uv sync
cp .env.example .env  # fill in secrets
uv run pre-commit install
uv run alembic upgrade head   # apply DB migrations
```

## Run locally

```bash
uv run python -m trader             # trading worker
uv run python -m tgbot              # telegram bot
uv run python -m webui              # read-only dashboard (WEBUI_HOST/PORT)
uv run python -m cli preflight      # validate config/credentials/balance
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
uv run python -m cli add-admin <chat_id> --label "Owner"
```

Send `/start` to the bot from that chat — only admins listed in `TelegramUser` can use commands.

## CI/CD

GitHub Actions (`.github/workflows/ci.yml`) runs on push/PR to `main`:
- `ruff check`, `ruff format --check`
- `mypy --strict`
- `pytest` (unit, integration excluded)

Integration tests against Bybit testnet are kept out of CI by default — run them locally
with `BYBIT_API_KEY=... BYBIT_API_SECRET=... BYBIT_TESTNET=1 uv run pytest -m integration tests/integration`.

## Pre-flight & dry-run

Before placing real orders, run the validator (checks Bybit creds, balance, instrument, Redis):

```bash
uv run python -m cli preflight
```

Set `TRADER_DRY_RUN=1` to have the trader log intended orders without placing them:

```bash
TRADER_DRY_RUN=1 uv run python -m trader
```

Full smoke-test walkthrough: see `docs/DEPLOY.md`.

## Railway deployment

Two long-running processes off the same repo, sharing one Postgres + Redis:

| Process | Start command |
|---------|---------------|
| `trader` | `python -m trader` |
| `tgbot` | `python -m tgbot` |

Shared env vars:

- `DATABASE_URL`, `REDIS_URL`
- `BYBIT_API_KEY`, `BYBIT_API_SECRET`, `BYBIT_TESTNET`
- `TELEGRAM_BOT_TOKEN`

First-run:
1. `uv run alembic upgrade head`
2. `python -m cli preflight` — validate credentials, balance, and config
3. `python -m cli add-admin <your_chat_id> --label "Owner"`
5. Restart `trader` service to pick up config

Health check on `web`: `GET /healthz` (unauthenticated, no DB).

## Strategy

See `/home/grenkoff/.claude/plans/velvet-sprouting-lampson.md` for the full plan.
