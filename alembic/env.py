"""Alembic configuration."""

import os
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

# Import Base directly to avoid loading settings
from telemon.database.models.base import Base

# Import all models to register them with Base.metadata
from telemon.database.models.user import User
from telemon.database.models.species import PokemonSpecies
from telemon.database.models.pokemon import Pokemon
from telemon.database.models.move import Move, PokemonLearnset
from telemon.database.models.item import Item, InventoryItem
from telemon.database.models.group import Group
from telemon.database.models.spawn import ActiveSpawn
from telemon.database.models.trade import Trade, TradeHistory
from telemon.database.models.market import MarketListing
from telemon.database.models.battle import Battle
from telemon.database.models.pokedex import PokedexEntry
from telemon.database.models.spawn_admin import SpawnAdmin
from telemon.database.models.quest import UserQuest
from telemon.database.models.wondertrade import WonderTrade
from telemon.database.models.achievement import UserAchievement
from telemon.database.models.breeding import DaycareSlot, PokemonEgg
from telemon.database.models.team import Team
from telemon.database.models.bot_config import BotConfig

# Alembic Config object
config = context.config

# Get database URL from environment or use default
database_url = os.environ.get(
    "DATABASE_URL",
    "postgresql+asyncpg://telemon:telemon@localhost:5434/telemon"
)

# Fix Heroku's URL formatting for the asyncpg driver
if database_url:
    # 1. Fix the protocol prefix
    if database_url.startswith("postgres://"):
        database_url = database_url.replace("postgres://", "postgresql+asyncpg://", 1)
    elif database_url.startswith("postgresql://") and not database_url.startswith("postgresql+asyncpg://"):
        database_url = database_url.replace("postgresql://", "postgresql+asyncpg://", 1)
        
    # 2. Fix the SSL argument for asyncpg
    if "sslmode=" in database_url:
        database_url = database_url.replace("sslmode=", "ssl=")

config.set_main_option("sqlalchemy.url", database_url)

# Interpret the config file for Python logging
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Model metadata for autogenerate
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    """Run migrations with connection."""
    context.configure(connection=connection, target_metadata=target_metadata)

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Run migrations in async mode."""
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""
    import asyncio

    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
