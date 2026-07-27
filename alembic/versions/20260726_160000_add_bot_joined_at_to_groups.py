"""add bot_joined_at to groups

The Group model declares `bot_joined_at` (used by the time-based spawn loop to
decide when a group became eligible for spawns), but no migration ever added
the column. Every SELECT against `groups` therefore failed with
UndefinedColumnError, breaking both the timed and incense spawn loops.

Backfilled from `created_at` so existing groups get a sensible eligibility
date rather than NULL.

Revision ID: b7d41e9c2a55
Revises: a3f2c8d91b00
Create Date: 2026-07-26 16:00:00.000000+00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b7d41e9c2a55'
down_revision: Union[str, None] = 'a3f2c8d91b00'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('groups', sa.Column('bot_joined_at', sa.DateTime(), nullable=True))
    # Existing rows predate the column; created_at is the closest truth we have
    # for when the bot started tracking the group.
    op.execute('UPDATE groups SET bot_joined_at = created_at WHERE bot_joined_at IS NULL')


def downgrade() -> None:
    op.drop_column('groups', 'bot_joined_at')
