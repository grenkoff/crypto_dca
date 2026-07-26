---
description: Review recent changes against Python/SQLAlchemy cookbook best practices that a linter can't check. Advisory, non-blocking.
---

Review the current change set against **Python / SQLAlchemy cookbook** best
practices — the idiomatic recipes that are *not* already caught by the ruff
rule groups in `/qa`. This is a **judgement pass, not a gate**: it surfaces
suggestions, it never blocks. Run it after `/qa` is green. A FastAPI web UI is
planned (Django-removal Phase 5) but not built yet.

## Scope

Only what changed on this branch:

- `git diff main...HEAD` for committed work, plus `git diff` / `git diff
  --staged` for uncommitted changes.
- Ignore `**/migrations/**` (except to sanity-check migration hygiene) and
  weigh `tests/**` lightly.

## What to check (recipes a linter misses)

- **Settings / config** — secrets, hosts, keys read from env/settings, never
  hardcoded; environment-specific values not baked into code.
- **ORM efficiency** — N+1 queries (loop that hits the DB per item); pulling
  whole rows when a column or count suffices (`select(Model.col)`,
  `select(func.count(...))`, `select(Model.id).where(...)` for existence);
  missing indexes on filtered/ordered fields.
- **Async correctness** — DAO stays native async on `AsyncSession` (no
  `sync_to_async`); a multi-write unit not wrapped in `session.begin()` (also
  an ACID-gate concern); blocking I/O on the event loop.
- **Migrations** — Alembic: more than one logical change in a migration; an
  edit to an already-applied migration; an irreversible migration without
  cause.
- **DAO idioms** — all ORM access stays behind `core.services.repository`;
  re-fetch with `with_for_update=True` for read-modify-write; idempotency on
  `exec_id`.
- **Logging** — `structlog` structured events (kwargs), not `print` or
  f-string-formatted messages; `log.exception` in `except` blocks.
- **Comprehensions** — a loop that only builds a collection should be a
  list/dict/set comprehension (or `.extend`); flag it. But leave a plain loop
  when the body has side effects or is too complex to read as a comprehension.
- **`match`** — an `if/elif` ladder dispatching on an enum/`Literal`, or code
  destructuring a tuple/dataclass, often reads clearer as `match`; suggest it
  there. Do *not* suggest `match` for 2-branch or range conditions — `if`
  wins.

## Output

A short, ranked list. For each item:

- **file:line** and the recipe it relates to.
- One sentence on the issue.
- A concrete **before → after** sketch, and the trade-off (why it's worth it
  *here*).

If nothing is warranted, say so plainly: **"No cookbook changes warranted —
the change already follows the recipes."** Don't invent findings to fill the
list; "no change" is the right answer when the code is already idiomatic.
