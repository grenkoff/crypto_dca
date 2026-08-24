"""Record funding transfers so account value can be rebuilt.

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c3d4e5f6a7b8"
down_revision: str | Sequence[str] | None = "b2c3d4e5f6a7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create ``trading_transfer``."""
    op.create_table(
        "trading_transfer",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("external_id", sa.String(64), nullable=False),
        sa.Column("coin", sa.String(16), nullable=False),
        sa.Column("amount", sa.Numeric(28, 12), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "external_id", name="trading_transfer_external_key"
        ),
    )
    op.create_index("trading_transfer_at_idx", "trading_transfer", ["at"])


def downgrade() -> None:
    """Drop ``trading_transfer``."""
    op.drop_index("trading_transfer_at_idx", table_name="trading_transfer")
    op.drop_table("trading_transfer")
