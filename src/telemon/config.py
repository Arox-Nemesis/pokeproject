"""Configuration settings for PokeVault."""

from functools import lru_cache
from typing import Literal

from pydantic import AnyUrl, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# ------------------------------------------------------------------ #
# Branding — change these three to rename the bot everywhere at once
# ------------------------------------------------------------------ #
BOT_NAME: str = "PokeVault"
CURRENCY_NAME: str = "PokeCoins"
CURRENCY_SHORT: str = "PC"

# ------------------------------------------------------------------ #
# Access control
# ------------------------------------------------------------------ #
BOT_OWNER_ID: int = 6894738352


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Bot Configuration
    bot_token: str = Field(..., description="Telegram Bot API token")
    bot_username: str = Field(default="pokevault_bot", description="Bot username")

    # Access Control
    group_only_enabled: bool = Field(
        default=True,
        description="When enabled, bot commands and callbacks are only processed in groups.",
    )
    force_sub_enabled: bool = Field(
        default=False,
        description="Require every user to belong to FORCE_SUB_CHAT_ID before using the bot.",
    )
    force_sub_chat_id: int | str | None = Field(
        default=None,
        description="Telegram chat/channel ID or @username users must join before using the bot.",
    )
    force_sub_url: AnyUrl | None = Field(
        default=None,
        description="Public invite/username URL shown when a user is not subscribed.",
    )

    # Database Configuration
    database_url: str = Field(
        default="postgresql+asyncpg://telemon:telemon@localhost:5434/telemon",
        description="PostgreSQL connection URL",
    )
    redis_url: str = Field(
        default="redis://localhost:6380/0",
        description="Redis connection URL",
    )

    # Spawning Configuration
    spawn_threshold_min: int = Field(default=20, ge=1, le=1000)
    spawn_threshold_max: int = Field(default=30, ge=1, le=1000)
    spawn_time_min_minutes: int = Field(default=5, ge=1)
    spawn_time_max_minutes: int = Field(default=15, ge=1)
    spawn_timeout_seconds: int = Field(default=300, ge=30)  # 5 minutes
    spawn_min_message_length: int = Field(default=3, ge=1)  # Min chars to count
    spawn_user_cooldown_seconds: float = Field(default=1.5, ge=0)  # Per-user cooldown
    spawn_guild_cooldown_seconds: float = Field(default=1.0, ge=0)  # Per-guild cooldown

    # Economy Configuration
    daily_reward_base: int = Field(default=100, ge=1)
    daily_streak_bonus: int = Field(default=10, ge=0)
    daily_streak_max: int = Field(default=30, ge=1)
    catch_reward_min: int = Field(default=10, ge=0)
    catch_reward_max: int = Field(default=100, ge=1)
    market_fee_percent: int = Field(default=5, ge=0, le=50)

    # Battle Configuration
    battle_turn_timeout_seconds: int = Field(default=60, ge=10)

    # Incense Configuration
    incense_spawn_count: int = Field(default=50, ge=1, le=500)

    # Shiny Configuration
    shiny_base_rate: int = Field(default=4096, ge=1)

    # Logging Configuration
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = Field(
        default="INFO"
    )
    log_format: Literal["console", "json"] = Field(default="console")

    # Development
    debug: bool = Field(default=False)

    @property
    def database_url_sync(self) -> str:
        """Get synchronous database URL for Alembic."""
        return str(self.database_url).replace("+asyncpg", "")


@lru_cache
def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()


# Global settings instance
settings = get_settings()
