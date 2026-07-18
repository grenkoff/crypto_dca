#!/usr/bin/env bash
# Project QA suite — run before finishing a task.
# PEP 8 + formatting, types, dead code, duplication (DRY), tests.
# Exits non-zero if any check fails.
set -u
cd "$(cd "$(dirname "$0")/.." && pwd)"

fail=0
run() {
  local name="$1"
  shift
  echo "=== ${name} ==="
  if "$@"; then
    echo "  ok"
  else
    echo "  FAIL (${name})"
    fail=1
  fi
}

run "ruff format --check (PEP 8)" .venv/bin/ruff format --check .
run "ruff check (PEP 8 + docstrings + unused + complexity)" \
  .venv/bin/ruff check .
run "mypy (types)" .venv/bin/mypy .
run "vulture (dead code)" .venv/bin/vulture
run "pylint duplicate-code (DRY)" \
  .venv/bin/pylint core tgbot trader web manage.py \
  --disable=all --enable=duplicate-code
run "check_transactions (ACID)" \
  .venv/bin/python scripts/check_transactions.py
run "pytest" .venv/bin/python -m pytest -q --ignore=tests/integration

echo
if [ "${fail}" -eq 0 ]; then
  echo "QA: ALL GREEN"
else
  echo "QA: FAILURES ABOVE — fix before finishing."
fi
exit "${fail}"
