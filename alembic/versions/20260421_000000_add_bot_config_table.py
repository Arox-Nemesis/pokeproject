"""Add bot_config table for persistent runtime config

Revision ID: a3f2c8d91b00
Revises: 1061fdf64fb1
Create Date: 2026-04-21 00:00:00.000000+00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a3f2c8d91b00'
down_revision: Union[str, None] = '1061fdf64fb1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'bot_config',
        sa.Column('key', sa.String(length=64), nullable=False),
        sa.Column('value', sa.Text(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint('key', name=op.f('pk_bot_config')),
    )


def downgrade() -> None:
    op.drop_table('bot_config')
