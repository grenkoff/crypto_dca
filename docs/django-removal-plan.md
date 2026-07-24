# Django removal plan — migrate ORM to SQLAlchemy, add FastAPI web UI

**Status:** planning — no code changed yet.
**Strategy:** incremental DAO-seam (not big-bang).
**Guiding invariant:** the Postgres schema does **not** change — SQLAlchemy
maps the existing tables, so there is **no data migration**, only code risk.

This is a rewrite of the persistence + transaction layer of a **live-money**
bot. The ACID discipline in `CLAUDE.md` (atomic multi-row writes, isolation via
`select_for_update`, idempotency on `exec_id`, durability for recovery) is
currently implemented with Django's transaction API and must be reproduced
faithfully in SQLAlchemy, or fills double-book / positions half-apply / races
appear.

---

## 1. Goal

- Remove Django entirely (ORM, migrations, settings, management commands,
  `web/`, django-stubs, pytest-django, dj-database-url).
- Replace the ORM with **SQLAlchemy 2.0 (async) + asyncpg + Alembic**.
- Replace management commands with **Typer** CLIs.
- Add **FastAPI + uvicorn** web UI / dashboard (the original ask), built on the
  now-Django-free data layer, served behind the Hetzner WireGuard VPN.

## 2. Current Django footprint (scan)

23 files import Django. Concentrations (django-flavoured lines):

| File | lines | layer |
|---|---|---|
| `tgbot/queries.py` | 21 | entrypoint |
| `core/services/order_manager.py` | 18 | **money core** |
| `core/services/repository.py` | 14 | DAO facade (partial) |
| `core/services/healer.py` | 12 | **money core** |
| `core/services/compensator.py` | 12 | **money core** |
| `core/services/grid_maintainer.py` | 9 | money core |
| `core/services/consolidate.py` | 9 | money core |
| `core/services/reconciliation.py` | 6 | money core |
| `core/services/protector.py` | 6 | money core |
| `tgbot/notify_settings.py`, `digest.py`, `notifications.py`, `filters.py` | 3–5 | entrypoint |
| `core/trading/models.py` | 9 model classes | schema |
| `core/trading/migrations/*` | 9 migrations | schema |
| `core/trading/management/commands/*` | 3 commands | entrypoint |
| `web/settings.py`, `urls.py`, `wsgi.py`, `apps.py`, `bootstrap.py`, `manage.py` | — | framework |

Key counts: `transaction.atomic` ×5, `select_for_update` ×1, async ORM /
`sync_to_async` ~114 call sites.

## 3. Target stack

- **SQLAlchemy 2.0** async (`AsyncSession`, `async with session.begin()`),
  driver **asyncpg**. Plain SQLAlchemy, not SQLModel — the money core needs the
  mature transaction API.
- **Alembic** for migrations; baseline-stamp the current schema.
- **Typer** for the ex-management commands.
- **FastAPI + uvicorn[standard]** for the web UI.
- Removed: `django`, `django-stubs`, `pytest-django`, `dj-database-url`.

The pydantic settings we already have (`core/config/settings.py`, incl. the new
`DatabaseSettings`) stay and absorb what `web/settings.py` did.

## 4. The DAO seam (why incremental works)

Today ORM access is **scattered** across money-core and tgbot. The plan funnels
**all** DB access through a repository/DAO interface first (still on Django),
then swaps only the DAO implementation to SQLAlchemy. Callers
(`order_manager`, `healer`, `compensator`, …) keep calling the same functions;
only the implementation behind them changes.

Seam contract (illustrative — final shape TBD in Phase 2):

```
core/services/repository.py   # the ONLY module that touches the ORM
    get_open_position(pos_id) -> Position | None
    grid_state(...) -> ...
    record_fill(exec_id, ...) -> bool      # idempotent, atomic
    pause() / resume() / is_paused()
    ...
```

Once every ORM statement lives behind this seam, Phase 3 rewrites the seam
internals against `AsyncSession` with the ACID mapping below — untouched
callers, faithful semantics.

## 5. ACID mapping (Django → SQLAlchemy) — the critical table

| Concern | Django (now) | SQLAlchemy 2.0 (target) |
|---|---|---|
| Atomic unit | `with transaction.atomic():` | `async with session.begin():` |
| Row isolation | `qs.select_for_update()` | `select(...).with_for_update()` |
| Idempotency on `exec_id` | check-then-write in one atomic block | same, inside one `session.begin()` |
| No exchange I/O in txn | gate A (`check_transactions.py`) | rewrite gate for `session.begin()` |
| ≥2 writes must be atomic | gate B | rewrite gate for SQLAlchemy writes |
| Durability / recovery | read state from DB on reconnect | unchanged (same tables) |
| Singleton `BotStatus.load()` | get-or-create | upsert / get-or-create helper |

Every one of the 5 `atomic` blocks and the 1 `select_for_update` is ported
1:1 and covered by a test that asserts the invariant (no half-applied state,
no double-book on redelivered `exec_id`).

## 6. Phases (each phase = its own PR, CI green, prod-safe unless noted)

### Phase 0 — scaffolding (Django still in place)
- Add `sqlalchemy[asyncio]`, `asyncpg`, `alembic`, `typer` to deps + lockfile.
- Declare SQLAlchemy `Base` + async engine/session bound to the existing
  `DATABASE_URL` (reuse `DatabaseSettings`).
- `alembic init`; configure `env.py` for async; **stamp** the current DB head so
  Alembic considers the live schema already applied (no DDL runs).
- Pytest async-DB fixture: create a disposable test DB, per-test rollback.
- Exit: `alembic current` matches live schema; empty SQLAlchemy metadata engine
  connects; fixtures work. Django untouched and still authoritative.

### Phase 1 — SQLAlchemy models mirroring existing tables
- Declare SA models for every table (`Position`, `GridLevel`, `ExecutionLog`,
  `CompensationLink`, `BotStatus`, `StrategyConfig`, `NotificationSettings`,
  `TelegramUser`, plus `auth_user`/`sessions` as needed) matching current
  columns/constraints exactly (`__tablename__`, types, nullability, indexes).
- Parity tests: read the same rows via Django and via SQLAlchemy → assert
  identical values on a copy of prod data.
- Alembic **autogenerate diff must be empty** vs the live schema (proof the SA
  models match Django's schema byte-for-byte).
- Exit: models verified equivalent; still no behaviour change in prod.

### Phase 2 — funnel all ORM access through the DAO (still Django)
- Move every ORM statement out of `order_manager`, `healer`, `compensator`,
  `grid_maintainer`, `consolidate`, `reconciliation`, `protector`,
  `tgbot/queries.py`, tgbot/* into `repository.py` (or a small `dao/` package).
- Pure refactor: behaviour identical, tests stay green, ship to prod in slices.
- Enforce with import-linter: only the DAO module may import the ORM.
- Exit: `grep` shows zero ORM usage outside the DAO seam.

### Phase 3 — swap DAO internals Django → SQLAlchemy (money core)
- Reimplement each DAO function against `AsyncSession` using the ACID mapping.
- Port all 5 `atomic` blocks + the `select_for_update` with invariant tests.
- Rewrite `scripts/check_transactions.py` gate to understand
  `async with session.begin()` instead of `transaction.atomic`.
- Callers unchanged. Async ORM `sync_to_async` ceremony disappears (native
  async) — remove wrappers as sites convert.
- Exit: full test suite + ACID invariant tests green on SQLAlchemy; trader runs
  on SQLAlchemy in `TRADER_DRY_RUN=1` against a prod-DB copy.

### Phase 4 — entrypoints + teardown
- `tgbot` fully on the DAO/SQLAlchemy path.
- Management commands → Typer CLIs (`preflight`, `consolidate_positions`,
  `add_tg_admin`).
- Delete `web/` (settings/urls/wsgi), `manage.py`, `core/trading/apps.py`,
  Django migrations, `core/config/bootstrap.py` Django bits.
- Remove deps: `django`, `django-stubs`, `pytest-django`, `dj-database-url`.
- Update `pyproject.toml`: drop django mypy plugin, django-stubs config,
  `DJANGO_SETTINGS_MODULE`; fix ruff `DJ`/pydocstyle scope; update import-linter
  contracts (entrypoints now `trader`/`tgbot`/`webui` over `core`).
- Update `CLAUDE.md`: remove Django/ORM-specific rules, replace "FastAPI is not
  used here", restate the ACID gate in SQLAlchemy terms.
- Regenerate `whitelist_vulture.py` (Django false-positives gone).
- Exit: no `import django` anywhere; `/qa` green; trader live on SQLAlchemy.

### Phase 5 — FastAPI web UI / dashboard
- New `webui/` entrypoint (uvicorn), binds to the WireGuard interface only.
- Read API + WebSocket live feed (subscribe existing Redis channel), then
  control endpoints (pause/resume/config) with SQLAlchemy `begin()` + auth +
  audit log. Built per the earlier dashboard plan, now on the clean data layer.

### Cutover (before Phase 4 teardown goes live)
- Long `TRADER_DRY_RUN=1` soak on a copy of the prod DB.
- Behaviour diff Django-path vs SQLAlchemy-path (same inputs → same DB writes).
- Switch during a maintenance stop (stop trader → deploy → restart), schema
  unchanged so rollback = redeploy the Django branch.

## 7. Tooling / gate changes

- `scripts/check_transactions.py` — rewrite AST checks for
  `async with session.begin()` and SQLAlchemy writes.
- `pyproject.toml` — remove django-stubs mypy plugin & `[tool.django-stubs]`,
  `DJANGO_SETTINGS_MODULE`, ruff `DJ` rules; update `[tool.importlinter]`.
- `pytest` — drop `pytest-django`; add async DB fixture + `pytest-asyncio`.
- CI — drop `manage.py check`; keep ruff/mypy/vulture/pylint/coverage; the
  Postgres service stays.
- `CLAUDE.md` — rewrite the ORM/ACID/cookbook sections in SQLAlchemy terms.

## 8. Risks & mitigations

| Risk | Mitigation |
|---|---|
| Broken ACID → double-book / half-apply on live money | schema unchanged; 1:1 ACID port; invariant tests; dry-run soak; maintenance-stop cutover |
| Huge untested diff | incremental DAO seam, one phase per PR, behaviour-preserving refactor first |
| Subtle SA↔Django schema mismatch | Alembic empty-autogenerate proof + Django/SA parity read tests |
| Recovery/idempotency regressions on WS redelivery | dedicated `exec_id` idempotency tests before cutover |
| Rollback | Django branch kept; DB never changes, so redeploy = instant rollback |

## 9. Open questions

- SA models: hand-write (chosen) vs `sqlacodegen` bootstrap then curate?
- Keep `auth_user`/`sessions` tables, or drop Django auth entirely and move to a
  token/TelegramUser-only auth for the web UI?
- Typer vs plain `argparse` for the 3 CLIs (Typer chosen unless objection).
- Web UI frontend: light SPA vs server-rendered + htmx (deferred to Phase 5).
