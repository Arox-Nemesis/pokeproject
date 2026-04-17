"""Pokemon instance model - user-owned Pokemon."""

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from telemon.core.constants import MAX_IV_TOTAL
from telemon.database.models.base import Base


class Pokemon(Base):
    """Represents a user-owned Pokemon instance."""

    __tablename__ = "pokemon"

    # Primary key
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )

    # Owner relationship
    owner_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.telegram_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Species reference
    species_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("pokemon_species.national_dex"),
        nullable=False,
        index=True,
    )

    # Nickname
    nickname: Mapped[str | None] = mapped_column(String(50), nullable=True)

    # Level and experience
    level: Mapped[int] = mapped_column(Integer, default=1)
    experience: Mapped[int] = mapped_column(BigInteger, default=0)

    # Individual Values (IVs) - 0 to 31
    iv_hp: Mapped[int] = mapped_column(Integer, default=0)
    iv_attack: Mapped[int] = mapped_column(Integer, default=0)
    iv_defense: Mapped[int] = mapped_column(Integer, default=0)
    iv_sp_attack: Mapped[int] = mapped_column(Integer, default=0)
    iv_sp_defense: Mapped[int] = mapped_column(Integer, default=0)
    iv_speed: Mapped[int] = mapped_column(Integer, default=0)

    # Effort Values (EVs) - 0 to 252, max total 510
    ev_hp: Mapped[int] = mapped_column(Integer, default=0)
    ev_attack: Mapped[int] = mapped_column(Integer, default=0)
    ev_defense: Mapped[int] = mapped_column(Integer, default=0)
    ev_sp_attack: Mapped[int] = mapped_column(Integer, default=0)
    ev_sp_defense: Mapped[int] = mapped_column(Integer, default=0)
    ev_speed: Mapped[int] = mapped_column(Integer, default=0)

    # Nature (affects stat growth)
    nature: Mapped[str] = mapped_column(String(20), default="hardy")

    # Ability
    ability: Mapped[str | None] = mapped_column(String(50), nullable=True)
    ability_slot: Mapped[int] = mapped_column(Integer, default=1)  # 1, 2, or 3 (hidden)

    # Shiny status
    is_shiny: Mapped[bool] = mapped_column(Boolean, default=False)

    # Moves (up to 4)
    moves: Mapped[list] = mapped_column(ARRAY(String(50)), default=list)

    # Held item
    held_item: Mapped[str | None] = mapped_column(String(50), nullable=True)

    # Friendship (0-255)
    friendship: Mapped[int] = mapped_column(Integer, default=70)

    # Flags
    is_favorite: Mapped[bool] = mapped_column(Boolean, default=False)
    is_on_market: Mapped[bool] = mapped_column(Boolean, default=False)
    is_in_trade: Mapped[bool] = mapped_column(Boolean, default=False)

    # Catch info
    original_trainer_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    caught_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)
    caught_in_group_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    # Gender (male, female, or None for genderless)
    gender: Mapped[str | None] = mapped_column(String(10), nullable=True)

    # Form variant (for Pokemon with multiple forms)
    form: Mapped[str | None] = mapped_column(String(50), nullable=True)

    # Relationships
    owner = relationship("User", back_populates="pokemon")
    species = relationship("PokemonSpecies", lazy="joined")

    def __repr__(self) -> str:
        return f"<Pokemon {self.id} {self.display_name} Lv.{self.level}>"

    @property
    def display_name(self) -> str:
        """Get display name (nickname or species name)."""
        if self.nickname:
            return self.nickname
        if self.species:
            return self.species.name
        return f"Pokemon #{self.species_id}"

    @property
    def iv_total(self) -> int:
        """Get total IV sum."""
        return (
            self.iv_hp
            + self.iv_attack
            + self.iv_defense
            + self.iv_sp_attack
            + self.iv_sp_defense
            + self.iv_speed
        )

    @property
    def iv_percentage(self) -> float:
        """Get IV percentage (out of perfect 186)."""
        return round((self.iv_total / MAX_IV_TOTAL) * 100, 2)

    @property
    def ev_total(self) -> int:
        """Get total EV sum."""
        return (
            self.ev_hp
            + self.ev_attack
            + self.ev_defense
            + self.ev_sp_attack
            + self.ev_sp_defense
            + self.ev_speed
        )

    @property
    def is_perfect_iv(self) -> bool:
        """Check if Pokemon has perfect IVs (all 31)."""
        return self.iv_total == MAX_IV_TOTAL

    @property
    def is_tradeable(self) -> bool:
        """Check if Pokemon can be traded."""
        return not self.is_favorite and not self.is_on_market and not self.is_in_trade

    @property
    def is_releasable(self) -> bool:
        """Check if Pokemon can be released."""
        return not self.is_favorite and not self.is_on_market and not self.is_in_trade
