"""add persistent per-user/per-group incense usage history

Revision ID: 20260915_incense_usage
Revises: b7d41e9c2a55
"""
from alembic import op
import sqlalchemy as sa

revision = "20260915_incense_usage"
down_revision = "b7d41e9c2a55"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "incense_usage",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("used_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.telegram_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_incense_usage_user_group_used_at",
        "incense_usage",
        ["user_id", "chat_id", "used_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_incense_usage_user_group_used_at", table_name="incense_usage")
    op.drop_table("incense_usage")
