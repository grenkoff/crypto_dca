"""Mark hand-adopted lots with a flag instead of a level-index range.

Revision ID: b8c9d0e1f2a3
Revises: a7b8c9d0e1f2
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b8c9d0e1f2a3"
down_revision: str | Sequence[str] | None = "a7b8c9d0e1f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ADOPTED_MIN = 1000
_ADOPTED_MAX = 2000


def upgrade() -> None:
    """Add ``adopted`` and backfill it from the old sentinel range.

    Lots taken over by hand were marked by a synthetic level index in
    [1000, 2000). A percent grid puts real levels in that range too, so
    the flag has to be its own column.
    """
    op.add_column(
        "trading_position",
        sa.Column(
            "adopted",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    positions = sa.table(
        "trading_position",
        sa.column("adopted", sa.Boolean),
        sa.column("level_index", sa.Integer),
    )
    op.execute(
        positions.update()
        .where(positions.c.level_index >= _ADOPTED_MIN)
        .where(positions.c.level_index < _ADOPTED_MAX)
        .values(adopted=True)
    )


def downgrade() -> None:
    """Drop the flag; the level-index range still identifies the lots."""
    op.drop_column("trading_position", "adopted")
