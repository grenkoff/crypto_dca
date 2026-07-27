"""webui control: telegram control_token + audit table

Adds the per-admin dashboard control token and the control audit trail.

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-07-26 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b2c3d4e5f6a7"
down_revision: str | Sequence[str] | None = "a1b2c3d4e5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add ``control_token`` and create ``webui_audit``."""
    op.add_column(
        "trading_telegramuser",
        sa.Column("control_token", sa.String(64), nullable=True),
    )
    op.create_table(
        "webui_audit",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("actor", sa.String(64), nullable=False),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("detail", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    """Drop ``webui_audit`` and ``control_token``."""
    op.drop_table("webui_audit")
    op.drop_column("trading_telegramuser", "control_token")
