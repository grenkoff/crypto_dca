---
description: Review recent changes against Python/Django cookbook best practices that a linter can't check. Advisory, non-blocking.
---

Review the current change set against **Python / Django cookbook** best
practices — the idiomatic recipes that are *not* already caught by the ruff
rule groups and `manage.py check` in `/qa`. This is a **judgement pass, not a
gate**: it surfaces suggestions, it never blocks. Run it after `/qa` is green.
FastAPI is not used in this project — ignore FastAPI recipes.

## Scope

Only what changed on this branch:

- `git diff main...HEAD` for committed work, plus `git diff` / `git diff
  --staged` for uncommitted changes.
- Ignore `**/migrations/**` (except to sanity-check migration hygiene) and
  weigh `tests/**` lightly.

## What to check (recipes a linter misses)

- **Settings / config** — secrets, hosts, keys read from env/settings, never
  hardcoded; environment-specific values not baked into code.
- **ORM efficiency** — N+1 queries (loop that hits the DB per item →
  `select_related`/`prefetch_related`); materializing a queryset just to count
  or test existence (use `.count()` / `.exists()`); pulling whole objects when
  `.values_list()` suffices; missing indexes on filtered/ordered fields.
- **Async correctness** — ORM touched from async code without `sync_to_async`
  or the `a*` API; a multi-write unit not wrapped in `transaction.atomic()`
  (also an ACID-gate concern); blocking I/O on the event loop.
- **Migrations** — more than one logical change in a migration; an edit to an
  already-applied migration; an irreversible migration without cause.
- **Django idioms** — fat models / thin views kept (business logic out of
  views/handlers); `get_object_or_404` style guards; `F()`/`Q()` for atomic
  updates and complex filters; `bulk_create`/`bulk_update` for batches.
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
