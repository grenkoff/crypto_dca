from __future__ import annotations

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import Engine

import core.db.models  # noqa: F401
from core.db.base import Base
from core.db.schema import include_name

pytestmark = pytest.mark.django_db(transaction=True)


def test_sa_models_match_django_schema(sa_sync_engine: Engine) -> None:
    with sa_sync_engine.connect() as conn:
        ctx = MigrationContext.configure(
            conn,
            opts={"include_name": include_name, "compare_type": True},
        )
        diff = compare_metadata(ctx, Base.metadata)
    assert diff == [], diff
