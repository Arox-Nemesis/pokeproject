"""Trade model for Pokemon trading."""

import uuid
from datetime import datetime
from enum import Enum

from sqlalchemy import BigInteger, Enum as SQLEnum, ForeignKey, Integer
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from telemon.database.models.base import Base


class TradeStatus(str, Enum):
    """Trade status enum."""

    WAITING_ACCEPT = "waiting_accept"  # Waiting for target user to accept
    PENDING = "pending"
    CONFIRMED_ONE = "confirmed_one"  # One party confirmed
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class Trade(Base):
    """Represents an active or completed trade."""

    __tablename__ = "trades"

    # Primary key
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )

    # Participants
    user1_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.telegram_id"),
        nullable=False,
        index=True,
    )
    user2_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.telegram_id"),
        nullable=False,
        index=True,
    )

    # Pokemon being traded (list of UUIDs as strings)
    user1_pokemon_ids: Mapped[list] = mapped_column(ARRAY(UUID(as_uuid=True)), default=list)
    user2_pokemon_ids: Mapped[list] = mapped_column(ARRAY(UUID(as_uuid=True)), default=list)

    # Coins being traded
    user1_coins: Mapped[int] = mapped_column(BigInteger, default=0)
    user2_coins: Mapped[int] = mapped_column(BigInteger, default=0)

    # Confirmation status
    user1_confirmed: Mapped[bool] = mapped_column(default=False)
    user2_confirmed: Mapped[bool] = mapped_column(default=False)

    # Trade status
    status: Mapped[TradeStatus] = mapped_column(
        SQLEnum(TradeStatus),
        default=TradeStatus.PENDING,
    )

    # Message tracking for UI
    chat_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    # Timing
    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)
    last_activity_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(nullable=True)

    # Relationships
    user1 = relationship("User", foreign_keys=[user1_id])
    user2 = relationship("User", foreign_keys=[user2_id])

    def __repr__(self) -> str:
        return f"<Trade {self.id} {self.user1_id} <-> {self.user2_id}>"

    @property
    def is_pending(self) -> bool:
        """Check if trade is pending."""
        return self.status == TradeStatus.PENDING

    @property
    def is_completed(self) -> bool:
        """Check if trade is completed."""
        return self.status == TradeStatus.COMPLETED

    @property
    def both_confirmed(self) -> bool:
        """Check if both parties have confirmed."""
        return self.user1_confirmed and self.user2_confirmed


class TradeHistory(Base):
    """Historical record of completed trades."""

    __tablename__ = "trade_history"

    # Primary key
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )

    # Original trade reference
    trade_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)

    # Participants
    user1_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    user2_id: Mapped[int] = mapped_column(BigInteger, nullable=False)

    # Pokemon count traded
    user1_pokemon_count: Mapped[int] = mapped_column(Integer, default=0)
    user2_pokemon_count: Mapped[int] = mapped_column(Integer, default=0)

    # Coins traded
    user1_coins: Mapped[int] = mapped_column(BigInteger, default=0)
    user2_coins: Mapped[int] = mapped_column(BigInteger, default=0)

    # Timestamp
    completed_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)
