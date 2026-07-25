"""Schema-comparison helper for Alembic autogenerate."""

from __future__ import annotations


def include_name(
    name: str | None, type_: str, _parent_names: dict[str, str | None]
) -> bool:
    """Limit Alembic schema comparison to the ``trading_*`` tables.

    Any residual non-``trading_*`` tables (legacy Django meta/auth,
    ``alembic_version``) are ignored so autogenerate does not try to
    drop tables the models do not declare.
    """
    if type_ == "table":
        return name is not None and name.startswith("trading_")
    return True
