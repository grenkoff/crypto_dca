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
