"""Persistent incense-use history for per-user/per-group cooldowns."""

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from telemon.database.models.base import Base


class IncenseUsage(Base):
    """One consumed group-incense activation.

    The history is intentionally separate from the active incense counter so
    the 12-hour allowance survives restarts and can be changed per group.
    """

    __tablename__ = "incense_usage"
    __table_args__ = (
        Index("ix_incense_usage_user_group_used_at", "user_id", "chat_id", "used_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.telegram_id", ondelete="CASCADE"), nullable=False
    )
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    used_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
