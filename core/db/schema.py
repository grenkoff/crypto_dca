"""Schema-comparison helpers shared by Alembic and the parity tests."""

from __future__ import annotations


def include_name(
    name: str | None, type_: str, _parent_names: dict[str, str | None]
) -> bool:
    """Limit Alembic schema comparison to the ``trading_*`` tables.

    The Django ``auth``/``sessions``/``contenttypes`` tables and
    ``alembic_version`` are ignored so autogenerate does not try to
    drop them while the schema is still Django-owned.
    """
    if type_ == "table":
        return name is not None and name.startswith("trading_")
    return True
