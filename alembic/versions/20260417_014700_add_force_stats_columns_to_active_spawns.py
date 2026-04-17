"""add force stats columns to active_spawns

Revision ID: a3c8f21db45e
Revises: 7834db8072fb
Create Date: 2026-04-17 01:47:00.000000+00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'a3c8f21db45e'
down_revision: Union[str, None] = '7834db8072fb'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('active_spawns', sa.Column('force_level', sa.Integer(), nullable=True))
    op.add_column('active_spawns', sa.Column('force_iv_hp', sa.Integer(), nullable=True))
    op.add_column('active_spawns', sa.Column('force_iv_attack', sa.Integer(), nullable=True))
    op.add_column('active_spawns', sa.Column('force_iv_defense', sa.Integer(), nullable=True))
    op.add_column('active_spawns', sa.Column('force_iv_sp_attack', sa.Integer(), nullable=True))
    op.add_column('active_spawns', sa.Column('force_iv_sp_defense', sa.Integer(), nullable=True))
    op.add_column('active_spawns', sa.Column('force_iv_speed', sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column('active_spawns', 'force_iv_speed')
    op.drop_column('active_spawns', 'force_iv_sp_defense')
    op.drop_column('active_spawns', 'force_iv_sp_attack')
    op.drop_column('active_spawns', 'force_iv_defense')
    op.drop_column('active_spawns', 'force_iv_attack')
    op.drop_column('active_spawns', 'force_iv_hp')
    op.drop_column('active_spawns', 'force_level')
