"""Spawn admin model for users allowed to use /spawn command."""

from sqlalchemy import BigInteger, Text
from sqlalchemy.dialects.postgresql import ARRAY, VARCHAR
from sqlalchemy.orm import Mapped, mapped_column

from telemon.database.models.base import Base, TimestampMixin

# Valid spawn permissions
SPAWN_PERMISSIONS = {
    "name",      # /spawn Pikachu  — spawn by name
    "gen",       # /spawn gen:3    — filter by generation
    "type",      # /spawn type:fire — filter by type
    "rarity",    # /spawn legendary / mythical / rare / ultra_rare
    "shiny",     # /spawn --shiny  — force shiny
    "stats",     # /spawn level:50 iv:31 — force level/IVs
    "all",       # shortcut: grants everything
}


class SpawnAdmin(Base, TimestampMixin):
    """Users who are allowed to use /spawn command in any group."""

    __tablename__ = "spawn_admins"

    # User's Telegram ID
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)

    # Who added this user (bot owner or another admin)
    added_by: Mapped[int] = mapped_column(BigInteger, nullable=False)

    # Optional notes
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Granted spawn permissions (empty/null = random only)
    permissions: Mapped[list[str] | None] = mapped_column(
        ARRAY(VARCHAR(20)), nullable=True, default=list
    )

    # ------------------------------------------------------------------ #
    # helpers
    # ------------------------------------------------------------------ #
    def has_perm(self, perm: str) -> bool:
        """Check if this spawner has a specific permission."""
        if not self.permissions:
            return False
        return "all" in self.permissions or perm in self.permissions

    def perm_display(self) -> str:
        """Human-readable permission list."""
        if not self.permissions:
            return "random only"
        if "all" in self.permissions:
            return "all"
        return ", ".join(sorted(self.permissions))

    def __repr__(self) -> str:
        return f"<SpawnAdmin user_id={self.user_id} perms={self.permissions}>"
