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
```

## Run

```bash
uv run python manage.py runserver   # web
uv run python -m trader             # trading worker
uv run python -m tgbot              # telegram bot
```

## Checks

```bash
uv run ruff check
uv run ruff format --check
uv run mypy .
uv run pytest
```

## Strategy

See `/home/grenkoff/.claude/plans/velvet-sprouting-lampson.md` for the full plan.
