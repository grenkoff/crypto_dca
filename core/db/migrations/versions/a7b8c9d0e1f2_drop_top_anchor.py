"""Drop the grid anchor the percent ladder no longer needs.

Revision ID: a7b8c9d0e1f2
Revises: f6a7b8c9d0e1
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a7b8c9d0e1f2"
down_revision: str | Sequence[str] | None = "f6a7b8c9d0e1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Drop ``top_anchor``.

    The percent grid is pinned at one tick and counted upward, so it needs
    no stored anchor to keep its level indexes stable.
    """
    op.drop_column("trading_strategyconfig", "top_anchor")


def downgrade() -> None:
    """Restore the nullable anchor column."""
    op.add_column(
        "trading_strategyconfig",
        sa.Column("top_anchor", sa.Numeric(28, 12), nullable=True),
    )
