"""Record how much of a close's profit stayed in the pocket.

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f6a7b8c9d0e1"
down_revision: str | Sequence[str] | None = "e5f6a7b8c9d0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the nullable pocket share of a close.

    Left NULL on existing rows on purpose: the split did not exist when
    they closed, so their realized result is the best record of what
    stayed, and the chart falls back to it.
    """
    op.add_column(
        "trading_position",
        sa.Column("pocket_delta", sa.Numeric(28, 12), nullable=True),
    )


def downgrade() -> None:
    """Drop the pocket share column."""
    op.drop_column("trading_position", "pocket_delta")
