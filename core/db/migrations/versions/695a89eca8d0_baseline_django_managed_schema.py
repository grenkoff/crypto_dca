"""baseline django-managed schema

Revision ID: 695a89eca8d0
Revises:
Create Date: 2026-07-25 03:45:35.815897

"""

from collections.abc import Sequence

revision: str = "695a89eca8d0"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""


def downgrade() -> None:
    """Downgrade schema."""
