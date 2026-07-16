#!/usr/bin/env bash
# PostToolUse hook: keep edited Python files PEP 8 / 79-char compliant.
# Reads the hook JSON on stdin, formats + autofixes the touched .py file.
# Best-effort: never blocks the edit (always exits 0).

root="${CLAUDE_PROJECT_DIR:-$(pwd)}"
py="$root/.venv/bin/python"
ruff="$root/.venv/bin/ruff"

f=$("$py" -c "import sys, json; print(json.load(sys.stdin).get('tool_input', {}).get('file_path', '') or '')" 2>/dev/null)

case "$f" in
  *.py)
    "$ruff" format "$f" >/dev/null 2>&1
    "$ruff" check --fix "$f" >/dev/null 2>&1
    ;;
esac

exit 0
