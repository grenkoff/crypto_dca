"""Split closing profit between compensation and a pocket.

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d4e5f6a7b8c9"
down_revision: str | Sequence[str] | None = "c3d4e5f6a7b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the pocket balance and the two share bounds."""
    op.add_column(
        "trading_botstatus",
        sa.Column(
            "pocket_credit",
            sa.Numeric(28, 12),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "trading_strategyconfig",
        sa.Column(
            "comp_share_min",
            sa.Numeric(10, 8),
            nullable=False,
            server_default="0.20",
        ),
    )
    op.add_column(
        "trading_strategyconfig",
        sa.Column(
            "comp_share_max",
            sa.Numeric(10, 8),
            nullable=False,
            server_default="0.80",
        ),
    )


def downgrade() -> None:
    """Drop the pocket balance and the share bounds."""
    op.drop_column("trading_strategyconfig", "comp_share_max")
    op.drop_column("trading_strategyconfig", "comp_share_min")
    op.drop_column("trading_botstatus", "pocket_credit")
