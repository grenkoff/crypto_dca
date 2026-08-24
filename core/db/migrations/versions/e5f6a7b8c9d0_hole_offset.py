"""Add the gap-fill offset used when spending the credit pool.

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e5f6a7b8c9d0"
down_revision: str | Sequence[str] | None = "d4e5f6a7b8c9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add ``comp_hole_offset``."""
    op.add_column(
        "trading_strategyconfig",
        sa.Column(
            "comp_hole_offset",
            sa.Numeric(10, 8),
            nullable=False,
            server_default="0.03",
        ),
    )


def downgrade() -> None:
    """Drop ``comp_hole_offset``."""
    op.drop_column("trading_strategyconfig", "comp_hole_offset")
