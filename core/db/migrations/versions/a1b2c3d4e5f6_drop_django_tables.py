"""drop django auth/meta tables

Removes the tables Django owned that nothing depends on anymore (web auth
is token/TelegramUser-only). The ``trading_*`` tables stay untouched.

Revision ID: a1b2c3d4e5f6
Revises: 695a89eca8d0
Create Date: 2026-07-26 00:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

revision: str = "a1b2c3d4e5f6"
down_revision: str | Sequence[str] | None = "695a89eca8d0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DJANGO_TABLES = (
    "django_admin_log",
    "auth_user_user_permissions",
    "auth_user_groups",
    "auth_group_permissions",
    "auth_permission",
    "auth_group",
    "auth_user",
    "django_content_type",
    "django_session",
    "django_migrations",
)


def upgrade() -> None:
    """Drop the Django-owned auth/meta tables."""
    for table in _DJANGO_TABLES:
        op.execute(f'DROP TABLE IF EXISTS "{table}" CASCADE')


def downgrade() -> None:
    """Irreversible: the Django tables are not recreated."""
    raise NotImplementedError("dropping Django tables is not reversible")
