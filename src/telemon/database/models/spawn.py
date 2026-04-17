"""Spawn model for tracking active Pokemon spawns."""

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, ForeignKey, Integer
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from telemon.database.models.base import Base


class ActiveSpawn(Base):
    """Represents an active Pokemon spawn in a group."""

    __tablename__ = "active_spawns"

    # Primary key
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )

    # Group where spawn occurred
    chat_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("groups.chat_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Pokemon species that spawned
    species_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("pokemon_species.national_dex"),
        nullable=False,
    )

    # Message ID for the spawn message
    message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)

    # Shiny status
    is_shiny: Mapped[bool] = mapped_column(Boolean, default=False)

    # Timing
    spawned_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)
    expires_at: Mapped[datetime] = mapped_column(nullable=False)

    # Catch info (filled when caught)
    caught_by: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("users.telegram_id"),
        nullable=True,
    )
    caught_at: Mapped[datetime | None] = mapped_column(nullable=True)

    # Hint state (how many letters revealed)
    hints_used: Mapped[int] = mapped_column(Integer, default=0)

    # Admin-forced stats (nullable — None means random at catch time)
    force_level: Mapped[int | None] = mapped_column(Integer, nullable=True)
    force_iv_hp: Mapped[int | None] = mapped_column(Integer, nullable=True)
    force_iv_attack: Mapped[int | None] = mapped_column(Integer, nullable=True)
    force_iv_defense: Mapped[int | None] = mapped_column(Integer, nullable=True)
    force_iv_sp_attack: Mapped[int | None] = mapped_column(Integer, nullable=True)
    force_iv_sp_defense: Mapped[int | None] = mapped_column(Integer, nullable=True)
    force_iv_speed: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Relationships
    species = relationship("PokemonSpecies", lazy="joined")
    group = relationship("Group", lazy="joined")

    def __repr__(self) -> str:
        return f"<ActiveSpawn {self.id} species={self.species_id} in {self.chat_id}>"

    @property
    def is_active(self) -> bool:
        """Check if spawn is still active."""
        return self.caught_by is None and datetime.utcnow() < self.expires_at

    @property
    def is_caught(self) -> bool:
        """Check if spawn was caught."""
        return self.caught_by is not None

    @property
    def is_expired(self) -> bool:
        """Check if spawn has expired."""
        return datetime.utcnow() >= self.expires_at and self.caught_by is None
