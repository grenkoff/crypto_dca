---
description: Run the full QA suite (PEP 8, types, dead code, DRY, tests) and fix findings.
---

Run the project QA suite and get it fully green before reporting the task done.

Run `bash scripts/qa.sh`. It checks, in order:

- `ruff format --check` + `ruff check` — PEP 8, mandatory docstrings, unused
  imports/variables, function complexity/size limits (`C901` + `PLR09xx`),
  security lint (`S` / flake8-bandit)
- `mypy` — types
- `vulture` — dead code (unused functions/classes/methods); framework
  false positives are whitelisted in `whitelist_vulture.py`
- `pylint duplicate-code` — DRY (copy-paste of ≥ 8 similar lines)
- `check_transactions` — ACID/transactions: no `await` inside
  `transaction.atomic()` (A), and ≥ 2 ORM writes in one function must be
  wrapped in `atomic()` (B)
- `import-linter` — layer boundaries (SoC): `services > exchange > strategy`,
  `strategy` stays pure, `core` never imports an entrypoint
- `pytest` — unit tests **+ coverage floor** on `core` (`fail_under`,
  ratcheted; a drop fails the run)

For each failure:

- **PEP 8 / format / types / unused / security (`S`)** — fix the code (add a
  request timeout, drop the hardcoded value, etc.). Don't blanket-ignore an
  `S` rule; if it's a real false positive, narrow it in `pyproject.toml` with
  a reason.
- **Coverage floor** — a drop means new logic landed without tests: add tests
  for it. Only raise `fail_under` (never lower it) once coverage improves.
- **Import contract broken** — a layer boundary was crossed (e.g. `strategy`
  importing `exchange`/ORM, or `core` importing an entrypoint). Fix the
  dependency direction (move the code or invert the dependency); do not relax
  the contract to make it pass.
- **Dead code (vulture)** — remove it if it is genuinely unused. If it is a
  framework false positive (Django `Command`/`Meta`/model fields, aiogram
  `@router` handlers, pydantic `model_config`, a tested public API method,
  etc.), append the reported name to `whitelist_vulture.py` (or regenerate:
  `.venv/bin/vulture <paths> --make-whitelist`), NOT into the real code.
- **Duplication (pylint)** — if it is the same knowledge, extract a shared
  helper; if it is coincidental (false DRY — similar code that changes for
  different reasons), leave it and say why.
- **Transactions (check_transactions)** — **A**: move the `await` out of the
  `atomic()` block (do exchange I/O before/after, never while holding the
  transaction). **B**: wrap the multiple writes in `transaction.atomic()`; only
  if they are genuinely independent, add `path.py:function` to
  `whitelist_transactions.txt` and say why.

Re-run until it prints `QA: ALL GREEN`, then summarise what you changed.
