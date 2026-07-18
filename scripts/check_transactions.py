#!/usr/bin/env python3
"""Static ACID/transaction checks for working code (AST, no runtime).

Two checks:

  A. No ``await`` inside a ``transaction.atomic()`` scope. Holding a DB
     transaction open across network I/O (e.g. a Bybit call) keeps row
     locks for the round-trip and mixes non-atomic external side effects
     into the unit of work. Atomic blocks must be synchronous.

  B. A function that performs two or more ORM writes (save/create/update/
     delete/... ) must wrap them in ``transaction.atomic()`` (or be
     ``@transaction.atomic``), so a mid-way failure cannot leave a
     half-applied state. Genuinely independent multi-writes can be
     exempted in ``whitelist_transactions.txt`` (``path.py:function``).

Exit status is non-zero if any non-whitelisted violation is found.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

MUTATIONS = frozenset(
    {
        "save",
        "delete",
        "asave",
        "adelete",
        "create",
        "acreate",
        "update",
        "aupdate",
        "update_or_create",
        "aupdate_or_create",
        "get_or_create",
        "aget_or_create",
        "bulk_create",
        "bulk_update",
        "abulk_create",
        "abulk_update",
    }
)

DEFAULT_PATHS = ("core", "tgbot", "web", "trader", "manage.py")
WHITELIST_FILE = "whitelist_transactions.txt"


def _is_atomic_expr(expr: ast.expr) -> bool:
    return (
        isinstance(expr, ast.Call)
        and isinstance(expr.func, ast.Attribute)
        and expr.func.attr == "atomic"
    )


def _is_atomic_with(node: ast.With | ast.AsyncWith) -> bool:
    return any(_is_atomic_expr(item.context_expr) for item in node.items)


def _has_atomic_decorator(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    for dec in fn.decorator_list:
        target = dec.func if isinstance(dec, ast.Call) else dec
        if isinstance(target, ast.Attribute) and target.attr == "atomic":
            return True
        if isinstance(target, ast.Name) and target.id == "atomic":
            return True
    return False


def _awaits_in_scope(body: list[ast.stmt]) -> list[ast.Await]:
    """Await nodes lexically inside ``body``, not within nested functions."""
    found: list[ast.Await] = []

    def visit(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(
                child,
                (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda),
            ):
                continue
            if isinstance(child, ast.Await):
                found.append(child)
            visit(child)

    for stmt in body:
        visit(stmt)
    return found


def _unguarded_mutations(
    body: list[ast.stmt], start_atomic: bool
) -> list[ast.Call]:
    """Mutation calls in ``body`` not under atomic, skipping nested funcs."""
    found: list[ast.Call] = []

    def visit(node: ast.AST, atomic: bool) -> None:
        if isinstance(node, (ast.With, ast.AsyncWith)):
            atomic = atomic or _is_atomic_with(node)
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if (
                isinstance(child, ast.Call)
                and isinstance(child.func, ast.Attribute)
                and child.func.attr in MUTATIONS
                and not atomic
            ):
                found.append(child)
            visit(child, atomic)

    for stmt in body:
        visit(stmt, start_atomic)
    return found


def _iter_functions(
    tree: ast.AST,
) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]


def _iter_atomic_withs(tree: ast.AST) -> list[ast.With | ast.AsyncWith]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.With, ast.AsyncWith))
        and _is_atomic_with(node)
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
    violations: list[str] = []

    for node in _iter_atomic_withs(tree):
        violations.extend(
            f"{path}:{await_node.lineno}: [A] await inside "
            "transaction.atomic() — atomic blocks must be synchronous"
            for await_node in _awaits_in_scope(node.body)
        )
    for fn in _iter_functions(tree):
        if not _has_atomic_decorator(fn):
            continue
        violations.extend(
            f"{path}:{await_node.lineno}: [A] await inside "
            "@transaction.atomic function — must be synchronous"
            for await_node in _awaits_in_scope(fn.body)
        )

    for fn in _iter_functions(tree):
        start_atomic = _has_atomic_decorator(fn)
        muts = _unguarded_mutations(fn.body, start_atomic)
        if len(muts) >= 2:
            key = f"{path}:{fn.name}"
            if key in whitelist:
                continue
            lines = ", ".join(str(m.lineno) for m in muts)
            violations.append(
                f"{path}:{fn.lineno}: [B] {len(muts)} unguarded ORM writes "
                f"in '{fn.name}' (lines {lines}) — wrap in "
                "transaction.atomic() or whitelist"
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
