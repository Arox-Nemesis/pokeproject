"""Configuration settings for PokeVault."""

from functools import lru_cache
from typing import Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import AnyUrl, Field, field_validator
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
    telegram_api_id: int | None = Field(
        default=None,
        description=(
            "Optional Telegram API ID for MTProto/client integrations; "
            "not required for the aiogram Bot API runtime."
        ),
    )
    telegram_api_hash: str | None = Field(
        default=None,
        description=(
            "Optional Telegram API hash for MTProto/client integrations; "
            "not required for the aiogram Bot API runtime."
        ),
    )

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

    @field_validator("database_url", mode="before")
    @classmethod
    def normalize_database_url(cls, value: str) -> str:
        """Normalize Heroku/Postgres URLs to SQLAlchemy asyncpg URLs."""
        if not value:
            return value

        database_url = str(value)
        if database_url.startswith("postgres://"):
            database_url = database_url.replace("postgres://", "postgresql+asyncpg://", 1)
        elif database_url.startswith("postgresql://"):
            database_url = database_url.replace("postgresql://", "postgresql+asyncpg://", 1)

        parts = urlsplit(database_url)
        query_items = parse_qsl(parts.query, keep_blank_values=True)
        normalized_query: list[tuple[str, str]] = []
        for key, item_value in query_items:
            if key == "sslmode":
                if item_value.lower() in {"require", "verify-ca", "verify-full"}:
                    normalized_query.append(("ssl", "true"))
                continue
            normalized_query.append((key, item_value))

        return urlunsplit(parts._replace(query=urlencode(normalized_query)))

    @field_validator("redis_url", mode="before")
    @classmethod
    def normalize_redis_url(cls, value: str) -> str:
        """Normalize managed Redis TLS URLs to redis-py rediss URLs."""
        if not value:
            return value

        redis_url = str(value)
        parts = urlsplit(redis_url)
        query_items = parse_qsl(parts.query, keep_blank_values=True)
        query = dict(query_items)
        wants_tls = (
            parts.scheme == "rediss"
            or parts.hostname is not None
            and parts.hostname.endswith("upstash.io")
            or query.get("ssl", "").lower() in {"1", "true", "yes", "require"}
            or query.get("tls", "").lower() in {"1", "true", "yes", "require"}
        )
        if not wants_tls or parts.scheme != "redis":
            return redis_url

        normalized_query = [
            (key, item_value)
            for key, item_value in query_items
            if key not in {"ssl", "tls"}
        ]
        return urlunsplit(
            parts._replace(scheme="rediss", query=urlencode(normalized_query))
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
