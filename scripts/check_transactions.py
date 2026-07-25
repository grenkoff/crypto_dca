#!/usr/bin/env python3
"""Static ACID/transaction checks for working code (AST, no runtime).

The DAO writes on SQLAlchemy ``AsyncSession``; the atomic unit is
``async with session.begin():``. Two checks:

  A. No non-database ``await`` inside a ``session.begin()`` scope. The
     transaction holds row locks; awaiting a Bybit round-trip (or any
     non-DB coroutine) inside it keeps locks open across network I/O and
     mixes a non-atomic external side effect into the unit of work. Only
     awaits on the session (``execute``/``flush``/``get``/``scalar``/...)
     and on local DAO helpers are allowed.

  B. A function performing two or more SQLAlchemy writes (``session.add``,
     ``session.delete``, ``session.execute(insert()/update()/delete())``)
     must wrap them in ``session.begin()``, so a mid-way failure cannot
     leave a half-applied state. Genuinely independent multi-writes can be
     exempted in ``whitelist_transactions.txt`` (``path.py:function``).

Exit status is non-zero if any non-whitelisted violation is found.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

SESSION_ASYNC = frozenset(
    {
        "execute",
        "flush",
        "get",
        "get_one",
        "scalar",
        "scalars",
        "merge",
        "refresh",
        "delete",
        "connection",
        "stream",
        "stream_scalars",
        "commit",
        "rollback",
        "close",
    }
)

WRITE_CONSTRUCTS = frozenset({"insert", "update", "delete"})

DEFAULT_PATHS = ("core", "tgbot", "web", "trader", "cli", "manage.py")
WHITELIST_FILE = "whitelist_transactions.txt"


def _is_begin_expr(expr: ast.expr) -> bool:
    return (
        isinstance(expr, ast.Call)
        and isinstance(expr.func, ast.Attribute)
        and expr.func.attr == "begin"
    )


def _is_begin_with(node: ast.With | ast.AsyncWith) -> bool:
    return any(_is_begin_expr(item.context_expr) for item in node.items)


def _local_func_names(tree: ast.AST) -> set[str]:
    return {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _is_write_call(node: ast.AST) -> bool:
    """Whether ``node`` is a SQLAlchemy write on a session."""
    if not (
        isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    ):
        return False
    attr = node.func.attr
    if attr in ("add", "add_all", "delete"):
        return True
    if attr == "execute" and node.args:
        arg = node.args[0]
        return (
            isinstance(arg, ast.Call)
            and isinstance(arg.func, ast.Name)
            and arg.func.id in WRITE_CONSTRUCTS
        )
    return False


def _is_db_safe_await(await_node: ast.Await, local_funcs: set[str]) -> bool:
    value = await_node.value
    if not isinstance(value, ast.Call):
        return False
    func = value.func
    if isinstance(func, ast.Attribute):
        return func.attr in SESSION_ASYNC
    if isinstance(func, ast.Name):
        return func.id in local_funcs
    return False


def _bad_awaits_in_scope(
    body: list[ast.stmt], local_funcs: set[str]
) -> list[ast.Await]:
    """Non-DB awaits inside ``body``, not within nested functions."""
    found: list[ast.Await] = []

    def visit(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(
                child,
                (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda),
            ):
                continue
            if isinstance(child, ast.Await) and not _is_db_safe_await(
                child, local_funcs
            ):
                found.append(child)
            visit(child)

    for stmt in body:
        visit(stmt)
    return found


def _unguarded_writes(body: list[ast.stmt]) -> list[ast.Call]:
    """SQLAlchemy writes in ``body`` not under begin(), skipping nested."""
    found: list[ast.Call] = []

    def visit(node: ast.AST, atomic: bool) -> None:
        if isinstance(node, (ast.With, ast.AsyncWith)):
            atomic = atomic or _is_begin_with(node)
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if (
                isinstance(child, ast.Call)
                and _is_write_call(child)
                and not atomic
            ):
                found.append(child)
            visit(child, atomic)

    for stmt in body:
        visit(stmt, False)
    return found


def _iter_functions(
    tree: ast.AST,
) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]


def _iter_begin_withs(tree: ast.AST) -> list[ast.With | ast.AsyncWith]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.With, ast.AsyncWith)) and _is_begin_with(node)
    ]


def _python_files(paths: list[str]) -> list[Path]:
    files: list[Path] = []
    for raw in paths:
        p = Path(raw)
        if p.is_file() and p.suffix == ".py":
            files.append(p)
        elif p.is_dir():
            files.extend(
                f for f in p.rglob("*.py") if "migrations" not in f.parts
            )
    return sorted(set(files))


def _load_whitelist() -> set[str]:
    path = Path(WHITELIST_FILE)
    if not path.exists():
        return set()
    entries: set[str] = set()
    for line in path.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            entries.add(line)
    return entries


def check_file(path: Path, whitelist: set[str]) -> list[str]:
    """Return transaction-check violations for one Python file."""
    tree = ast.parse(path.read_text(), filename=str(path))
    local_funcs = _local_func_names(tree)
    violations: list[str] = []

    for node in _iter_begin_withs(tree):
        violations.extend(
            f"{path}:{bad.lineno}: [A] non-DB await inside "
            "session.begin() — no exchange/network I/O in a transaction"
            for bad in _bad_awaits_in_scope(node.body, local_funcs)
        )

    for fn in _iter_functions(tree):
        writes = _unguarded_writes(fn.body)
        if len(writes) >= 2:
            key = f"{path}:{fn.name}"
            if key in whitelist:
                continue
            lines = ", ".join(str(w.lineno) for w in writes)
            violations.append(
                f"{path}:{fn.lineno}: [B] {len(writes)} unguarded SQLAlchemy "
                f"writes in '{fn.name}' (lines {lines}) — wrap in "
                "session.begin() or whitelist"
            )
    return violations


def main(argv: list[str]) -> int:
    """Run both checks over ``argv`` paths; return 1 if any violation."""
    paths = argv[1:] or list(DEFAULT_PATHS)
    whitelist = _load_whitelist()
    all_violations: list[str] = []
    for path in _python_files(paths):
        all_violations.extend(check_file(path, whitelist))
    if all_violations:
        for v in all_violations:
            print(v)
        print(f"\ncheck_transactions: {len(all_violations)} violation(s)")
        return 1
    print("check_transactions: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
