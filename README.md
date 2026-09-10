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
uv run python -m cli grid-geometry  # preview the percent grid ladder
uv run python -m cli trader-lease   # may a trader start? (exit 1 = lease held)
uv run python -m cli adopt          # put uncovered coin back to work
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

## Running the bot

Runs on the host (not a PaaS), sharing one Postgres + Redis. Use
`scripts/restart.sh [trader|tgbot]` to (re)launch a single detached instance
without leaving orphans. To switch the live bot to a new revision, follow
`docs/cutover-checklist.md`.

| Process | Start command |
|---------|---------------|
| `trader` | `python -m trader` |
| `tgbot` | `python -m tgbot` |
| `webui` | `python -m webui` (read-only dashboard + control) |

Shared env vars:

- `DATABASE_URL`, `REDIS_URL`
- `BYBIT_API_KEY`, `BYBIT_API_SECRET`, `BYBIT_TESTNET`
- `TELEGRAM_BOT_TOKEN`

First-run:
1. `uv run alembic upgrade head`
2. `python -m cli preflight` — validate credentials, balance, and config
3. `python -m cli add-admin <your_chat_id> --label "Owner"`
4. Restart `trader` service to pick up config

Dashboard (`python -m webui`) is read-only except the control actions
(`/control/pause` · `/control/resume` · `/control/config`), which require a
control token: send `/token` to the bot (admin-only) and use it as
`Authorization: Bearer <token>`.

Health check on `webui`: `GET /healthz` (unauthenticated, no DB).

## Grid geometry

Two spacings, picked by `grid_mode` in the strategy config:

- **`absolute`** — buys rest every `grid_step` in price, and a lot's
  take-profit sits `tp_step` above its entry. The profit a trade takes in
  percent therefore drifts with the price.
- **`percent`** — both steps are *fractions* of price, so neither drifts
  as the market moves: `grid_step` (e.g. `0.0011` = 0.11%) is how far the
  price falls between resting buys, and `tp_step` (e.g. `0.0066` = 0.66%)
  is the profit each lot takes — snapped up to the next buy rung, so the
  take-profit wall stays on the same lattice the compensator walks. The
  buy ladder is pinned at one tick and
  counted upward, which keeps level indexes stable and holds the ratio
  down to the tick — below `tick / grid_step` a rung widens to a single
  tick rather than stalling, so the grid keeps trading to the bottom.

Every coin in the wallet should be working. Coin no open lot accounts for
— a bag taken over by hand, or a close that booked a sale which never
reached the exchange — is swept into grid-sized lots at the market price
on the reconcile tick, each with the usual resting take-profit; the sweep
waits for the spare to survive a few ticks so a fill that has not rested
its sell yet is never double-booked. `GRID_AUTO_ADOPT=0` turns it off, and
`python -m cli adopt` does the same by hand with a dry-run first. Adopted
lots are flagged `adopted` and take level indexes from 1,000,000 up, clear
of every grid level.

Defaults live in `GRID_STEP_PCT` and `GRID_PROFIT_PCT` (see
`GridSettings`). `python -m cli grid-geometry` previews the ladder against
the live price; `--step X --tp Y` override the fractions and `--apply`
writes them to the config. Changing the geometry cancels and re-lays the
resting buys on the next trader start.

## Strategy

See `/home/grenkoff/.claude/plans/velvet-sprouting-lampson.md` for the full plan.
