"""User model for trainers."""

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from telemon.database.models.base import Base, TimestampMixin


class User(Base, TimestampMixin):
    """Represents a Telegram user/trainer."""

    __tablename__ = "users"

    # Primary key is Telegram user ID
    telegram_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)

    # User info
    username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    first_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_name: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Economy
    balance: Mapped[int] = mapped_column(BigInteger, default=0)

    # Daily rewards
    daily_streak: Mapped[int] = mapped_column(Integer, default=0)
    last_daily: Mapped[datetime | None] = mapped_column(nullable=True)

    # Selected Pokemon (UUID string reference)
    selected_pokemon_id: Mapped[str | None] = mapped_column(String(36), nullable=True)

    # Shiny hunting
    shiny_hunt_species_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    shiny_hunt_chain: Mapped[int] = mapped_column(Integer, default=0)

    # Battle stats
    battle_wins: Mapped[int] = mapped_column(Integer, default=0)
    battle_losses: Mapped[int] = mapped_column(Integer, default=0)
    battle_rating: Mapped[int] = mapped_column(Integer, default=1000)

    # Achievement tracking counters
    total_evolutions: Mapped[int] = mapped_column(Integer, default=0)
    total_trades: Mapped[int] = mapped_column(Integer, default=0)

    # Team membership
    team_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("teams.id", ondelete="SET NULL"), nullable=True
    )
    team_role: Mapped[str | None] = mapped_column(String(20), nullable=True)  # leader / officer / member

    # User settings (JSON)
    settings: Mapped[dict] = mapped_column(JSONB, default=dict)

    # Flags
    is_banned: Mapped[bool] = mapped_column(Boolean, default=False)
    ban_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Active boosts
    incense_spawns_remaining: Mapped[int] = mapped_column(Integer, default=0)
    xp_boost_until: Mapped[datetime | None] = mapped_column(nullable=True)

    # Relationships
    pokemon = relationship("Pokemon", back_populates="owner", lazy="dynamic")
    pokedex_entries = relationship("PokedexEntry", back_populates="user", lazy="dynamic")
    inventory_items = relationship("InventoryItem", back_populates="user", lazy="dynamic")
    team = relationship("Team", back_populates="members", lazy="selectin")
    market_listings = relationship(
        "MarketListing",
        foreign_keys="MarketListing.seller_id",
        back_populates="seller",
        lazy="dynamic"
    )

    def __repr__(self) -> str:
        return f"<User {self.telegram_id} @{self.username}>"

    @property
    def display_name(self) -> str:
        """Get display name for user."""
        if self.username:
            return f"@{self.username}"
        if self.first_name:
            return self.first_name
        return f"User {self.telegram_id}"

    @property
    def total_battles(self) -> int:
        """Get total number of battles."""
        return self.battle_wins + self.battle_losses

    @property
    def win_rate(self) -> float:
        """Get win rate as percentage."""
        if self.total_battles == 0:
            return 0.0
        return (self.battle_wins / self.total_battles) * 100
