# Project rules

## Code style — PEP 8 (strict)

- All Python code MUST follow PEP 8, **including the 79-character line limit**.
- This is enforced by ruff: `line-length = 79` with `E501` on (see
  `pyproject.toml`). CI runs `ruff format --check .`, `ruff check .`, `mypy .`.
- Before finishing ANY task that touches Python, run and get to clean:
  - `.venv/bin/ruff format .`
  - `.venv/bin/ruff check .`
  - `.venv/bin/mypy .`
- Keep every line ≤ 79 characters. When something is too long:
  - **strings** → split with implicit concatenation inside parentheses;
  - **comments / docstrings** → reflow the prose across lines;
  - **calls / signatures** → let `ruff format` wrap them.
- Prefer fixing the underlying issue over silencing it: don't let a trailing
  `# type: ignore` push a line past 79 — annotate/`cast` so the pragma isn't
  needed.

## Comments & docstrings

Applies to working code (`core/`, `tgbot/`, `web/`, `trader/`, `manage.py`) —
NOT tests or migrations.

- **No `#` comments at all** — none. Not even `# type: ignore` / `# noqa` /
  `# pragma` / shebang: fix the underlying issue (annotate, `cast`, move the
  import) instead of silencing it. (ruff can't enforce "zero comments"; it's a
  hard rule regardless.)
- **Every module, public class, and public function/method MUST have a
  docstring.** Enforced by ruff pydocstyle `D100`–`D103` + `D419` (see
  `pyproject.toml`); CI fails without them.
- **`__init__.py` files stay empty** (no docstring) — they are exempt (`D104`
  ignored). Don't add a package docstring.
- Each real module starts with a **module docstring** describing its purpose.
- Docstrings are **1–5 lines** and every line is **≤ 79 characters** (PEP 8).
  Keep them terse: a one-line summary, optionally a short blank-line-separated
  body. Do not exceed 5 lines — compress instead.
- Dunder methods, `__init__`, and Protocol stubs don't require docstrings
  (pragmatic scope); private (`_`-prefixed) names don't either.

## DRY & dead code

- Apply **DRY** while writing: don't repeat the same knowledge — extract a
  shared helper/constant. But avoid *false* DRY: code that only looks similar
  yet changes for different reasons must stay separate.
- **Delete dead code as you go** — unused functions/classes/imports/variables.
- Enforcement, three layers:
  1. **Per-edit hook** (`.claude/settings.json`) runs `ruff format` +
     `ruff check --fix` — PEP 8 and unused imports/variables, instantly.
  2. **End of task**: run `bash scripts/qa.sh` (or `/qa`) — adds `mypy`,
     `vulture` (dead code), `pylint duplicate-code` (DRY),
     `check_transactions` (ACID), `pytest` (+ coverage floor). Get to
     `QA: ALL GREEN`.
  3. **CI** blocks the merge on all of the above.
- **`vulture`** flags dead code. Django/aiogram/pydantic produce false
  positives (`Command`/`Meta`/model fields, `@router` handlers, `model_config`,
  tested public API); whitelist those in `whitelist_vulture.py` (regenerate
  with `.venv/bin/vulture <paths> --make-whitelist`), never hollow out real
  code to satisfy it. Real dead code → remove it.
- **`pylint --enable=duplicate-code`** flags copy-paste (≥ 8 similar lines);
  we use it instead of jscpd to avoid a Node toolchain. It only sees textual
  duplication — real DRY judgement is still yours.

## Data integrity — ACID / transactions

The DB is the source of truth (positions, grid levels, executions); the
exchange is an external, non-transactional system. Write with ACID in mind.

Judgement (write this way):

- **Atomicity** — a multi-step change is all-or-nothing. Any operation that
  writes more than one row/table as one logical unit (open a position + mark
  the level + log the execution) goes inside a single
  `with transaction.atomic():` block. Never leave a half-applied state.
- **Consistency** — never limp on with data that breaks an invariant; enforce
  it with model constraints/guards and fail fast. A sub-minimum notional, a
  missing TP, a negative qty is a raise, not a silent write.
- **Isolation** — when a row is read then written under possible concurrency
  (two fills racing one position/level), take `select_for_update()` inside the
  atomic block so the read-modify-write can't interleave. Keep transactions
  short.
- **Durability** — after commit the state must survive a crash/restart; that
  is *why* recovery reads from the DB. On reconnect the WS may redeliver, so
  fill handling is **idempotent on `exec_id`** (check-then-write in the same
  transaction) — never double-book.
- **The exchange is outside the transaction.** Do exchange I/O (place/cancel)
  *before or after* the atomic block, never while holding it — a network
  round-trip must not keep row locks open, and the exchange can't roll back.
  The mismatch this leaves is closed by the reconcile/heal layer
  (`reconcile_once`, `Healer`, `Compensator._restore_protection`), not by
  pretending the two systems are one unit.

Machine-enforced (a gate, like DRY) — `scripts/check_transactions.py`, in
`/qa` and CI:

- **A** — no `await` inside `transaction.atomic()` (atomic blocks stay
  synchronous; keep exchange I/O out of them).
- **B** — a function with ≥ 2 ORM writes must wrap them in
  `transaction.atomic()`. Genuinely-independent writes can be exempted in
  `whitelist_transactions.txt` (`path.py:function`) — prefer fixing over
  exempting.

## Security & supply chain

This bot holds live exchange API keys and moves real money, so treat security
as a gate, not an afterthought.

- **Never commit secrets.** Keys/tokens come from env/settings, never
  hardcoded. CI runs **gitleaks** (`secret-scan` job) over the diff and history
  — a hit blocks the merge. If one ever lands, rotate the key, don't just
  delete the commit.
- **Security lint** — ruff `S` (flake8-bandit) is in `select`: network calls
  need a `timeout`, no `shell=True`, no hardcoded credentials, no weak `random`
  for anything security-sensitive. `S101` (assert) is ignored on purpose —
  asserts are only mypy type-narrowing here. Fix findings; don't blanket-ignore
  a rule (narrow it in `pyproject.toml` with a reason if truly a false hit).
- **Dependencies** — `.github/dependabot.yml` opens weekly update PRs for the
  Python deps (uv) and the CI actions. Review and merge them; a known CVE in
  `pybit`/`aiogram`/`django` is your problem too. `uv sync --frozen` keeps
  builds reproducible from `uv.lock`.
- **Test coverage floor** — `pytest --cov` enforces `fail_under` on `core`
  (ratcheted like the complexity gates). A drop means money-path logic landed
  untested; add the tests rather than lowering the floor.

## Design principles

Machine-enforced (a gate, like DRY) — **complexity/size limits** via ruff:
`C901` (≤ 12), `PLR0913` (≤ 8 args), `PLR0911` (≤ 7 returns), `PLR0912`,
`PLR0915`. Thresholds are ratcheted to the current worst: they block *new*
bloat, they don't force refactoring existing code. A function that trips them
is usually doing too much — split it.

Judgement (no gate — write this way, can't be linted):

- **KISS** — the simplest thing that works; prefer boring over clever.
- **YAGNI** — build only what's needed now; no speculative abstraction.
- **Separation of concerns** — keep the layers apart (`strategy` = pure logic,
  `exchange` = I/O, `services` = orchestration, `tgbot`/`trader` = entrypoints).
  This one **is** gated: `import-linter` (`lint-imports`, in `/qa` and CI)
  enforces the layering — `core.services` > `core.exchange` > `core.strategy`,
  `strategy` imports no I/O/ORM, and nothing in `core` imports an entrypoint.
  Contracts live in `[tool.importlinter]` in `pyproject.toml`.
- **Fail fast** — raise on invalid state immediately (`ValueError`, guards),
  don't limp on with bad data.
- **Least astonishment** — code behaves the way a reader expects; no surprises.
- **Boy Scout Rule** — leave code cleaner than you found it (delete dead code,
  tidy nearby mess as you pass).
- **SOLID** — OOP-oriented, so apply it *where there are classes*; this
  codebase is mostly functional, so don't add abstraction just to satisfy a
  letter (KISS/YAGNI win):
  - **S** single responsibility — one reason to change per class/function.
  - **O** open/closed — extend by adding code, not editing working logic; but
    don't pre-build extension points you don't need.
  - **L** Liskov — an implementation must work anywhere its protocol/base is
    expected (`EventBus` impls, `DryRunBybitClient`).
  - **I** interface segregation — narrow protocols; don't force callers to
    depend on methods they don't use.
  - **D** dependency inversion — depend on abstractions and inject them
    (constructor DI in `OrderManager`/`TraderRuntime`).

## Design patterns (GoF)

Judgement, **no linter gate** — a pattern is a design choice, not an
invariant, so it can't be machine-checked. Use the GoF catalogue as shared
vocabulary and a toolbox, never as a target.

- **Apply a pattern only when it earns its place** — it must remove real
  duplication, decouple a real seam, or tame conditional logic that will keep
  growing. KISS/YAGNI break every tie. **Rule of Three**: don't abstract until
  the third case.
- **Prefer the lightweight Python idiom.** Many GoF patterns dissolve into
  first-class functions, a dict dispatch, `@dataclass`, a context manager, or
  duck typing. Reach for a class hierarchy only when that idiom stops scaling.
- **No speculative patterns.** A Strategy with one strategy, a Factory that
  makes one thing, an interface with one impl, indirection with one caller —
  that's over-engineering; delete it.

Smell → pattern (the trigger that justifies it):

| Smell in the change | Pattern to consider |
|---|---|
| growing `if/elif` on a type/mode picking behavior | **Strategy** (or dict of callables) |
| repeated conditional object construction | **Factory Method** / named `from_*` ctor |
| gluing an incompatible external API to ours | **Adapter** |
| cross-cutting behavior wrapping a client (dry-run, retry, log) | **Decorator/Proxy** — wrap, don't edit |
| callers reaching into a subsystem's internals | **Facade** |
| one action must notify N unrelated reactions | **Observer / pub-sub** (`EventBus`) |
| behavior depends on a lifecycle status with transitions | **State** (often an enum + guards suffices) |
| near-identical procedures differing in one step | **Template Method** (often just a callable arg) |
| operations needing undo/queue/replay/log | **Command** |

Already in the codebase (recognise, don't reinvent): **Strategy** (`EventBus`
impls, grid modes), **Observer** (`EventBus`), **Proxy/Decorator**
(`DryRunBybitClient`), **Facade** (`repository`, the runtime collaborators),
**Factory** (`BybitClient.from_settings`), **Singleton** (`BotStatus.load`),
**Command** (Django management commands).

After a design-touching change, run a **`/patterns`** review pass (advisory,
after `/qa` is green): for each significant hunk, ask "would a pattern here
remove real duplication/coupling?" *and* the reverse "is any pattern here
unnecessary?". For a large redesign, the **`pattern-reviewer`** subagent does
the same review in depth. Neither blocks a merge — they surface suggestions.
