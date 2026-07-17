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
     `vulture` (dead code), `pylint duplicate-code` (DRY), `pytest`. Get to
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
