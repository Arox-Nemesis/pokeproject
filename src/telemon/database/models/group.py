"""Group model for Telegram groups/chats."""

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from telemon.database.models.base import Base, TimestampMixin


class Group(Base, TimestampMixin):
    """Represents a Telegram group where the bot is active."""

    __tablename__ = "groups"

    # Primary key is Telegram chat ID
    chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)

    # Group info
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    username: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Spawning settings
    spawn_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    spawn_channel_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    spawn_threshold: Mapped[int] = mapped_column(Integer, default=24)

    # Message tracking for spawn triggers
    message_count: Mapped[int] = mapped_column(Integer, default=0)
    last_spawn_at: Mapped[datetime | None] = mapped_column(nullable=True)

    # When the bot joined this group (for time-based spawns)
    bot_joined_at: Mapped[datetime | None] = mapped_column(nullable=True)

    # Redirect settings (send bot replies to specific channel)
    redirect_channel_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    # Battle settings
    battles_enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    # Language
    language: Mapped[str] = mapped_column(String(10), default="en")

    # Group settings (JSON for flexibility)
    settings: Mapped[dict] = mapped_column(JSONB, default=dict)

    # Stats
    total_spawns: Mapped[int] = mapped_column(Integer, default=0)
    total_catches: Mapped[int] = mapped_column(Integer, default=0)

    # Flags
    is_premium: Mapped[bool] = mapped_column(Boolean, default=False)
    is_banned: Mapped[bool] = mapped_column(Boolean, default=False)
    ban_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Group incense (activated by admin)
    incense_until: Mapped[datetime | None] = mapped_column(nullable=True)
    incense_activated_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    def __repr__(self) -> str:
        return f"<Group {self.chat_id} {self.title}>"

    def increment_message_count(self) -> bool:
        """Increment message count and return True if spawn should trigger."""
        self.message_count += 1
        return self.message_count >= self.spawn_threshold

    def reset_message_count(self) -> None:
        """Reset message count after spawn."""
        self.message_count = 0
        self.last_spawn_at = datetime.utcnow()
