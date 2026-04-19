"""add_waiting_accept_to_tradestatus

Revision ID: 023385890446
Revises: c9e84ba34068
Create Date: 2026-04-19 02:56:34.965541+00:00

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "023385890446"
down_revision: Union[str, None] = "c9e84ba34068"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Use raw SQL to add the enum value. We set autocommit block manually
    # since ALTER TYPE cannot run inside a transaction block in Postgres.
    with op.get_context().autocommit_block():
        op.execute(
            "ALTER TYPE tradestatus ADD VALUE IF NOT EXISTS 'WAITING_ACCEPT' BEFORE 'PENDING'"
        )


def downgrade() -> None:
    # Postgres doesn't support removing values from an ENUM
    pass
