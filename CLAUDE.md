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
