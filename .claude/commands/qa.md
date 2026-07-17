---
description: Run the full QA suite (PEP 8, types, dead code, DRY, tests) and fix findings.
---

Run the project QA suite and get it fully green before reporting the task done.

Run `bash scripts/qa.sh`. It checks, in order:

- `ruff format --check` + `ruff check` — PEP 8, mandatory docstrings, unused
  imports/variables, function complexity/size limits (`C901` + `PLR09xx`)
- `mypy` — types
- `vulture` — dead code (unused functions/classes/methods); framework
  false positives are whitelisted in `whitelist_vulture.py`
- `pylint duplicate-code` — DRY (copy-paste of ≥ 8 similar lines)
- `pytest`

For each failure:

- **PEP 8 / format / types / unused** — fix the code.
- **Dead code (vulture)** — remove it if it is genuinely unused. If it is a
  framework false positive (Django `Command`/`Meta`/model fields, aiogram
  `@router` handlers, pydantic `model_config`, a tested public API method,
  etc.), append the reported name to `whitelist_vulture.py` (or regenerate:
  `.venv/bin/vulture <paths> --make-whitelist`), NOT into the real code.
- **Duplication (pylint)** — if it is the same knowledge, extract a shared
  helper; if it is coincidental (false DRY — similar code that changes for
  different reasons), leave it and say why.

Re-run until it prints `QA: ALL GREEN`, then summarise what you changed.
