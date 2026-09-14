"""Database session management."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from telemon.config import settings

_ASYNC_PG_STRIP_QUERY_KEYS = {"channel_binding", "gssencmode"}
_ASYNC_PG_SSL_QUERY_VALUES = {
    "1",
    "true",
    "require",
    "verify-ca",
    "verify-full",
}


def _make_asyncpg_url(database_url: str) -> URL:
    """Return a SQLAlchemy URL that always uses the asyncpg PostgreSQL driver."""
    if database_url.startswith("postgres://"):
        database_url = database_url.replace("postgres://", "postgresql://", 1)

    url = make_url(database_url)
    if url.drivername == "postgresql":
        url = url.set(drivername="postgresql+asyncpg")
    return url


def _build_database_engine_options(database_url: str | None = None) -> tuple[URL, dict]:
    """Build SQLAlchemy engine URL/options, adapting managed Postgres URLs for asyncpg."""
    url = _make_asyncpg_url(str(database_url or settings.database_url))
    connect_args = {}

    ssl_value = url.query.get("ssl") or url.query.get("sslmode")
    if isinstance(ssl_value, str) and ssl_value.lower() in _ASYNC_PG_SSL_QUERY_VALUES:
        connect_args["ssl"] = True
        url = url.difference_update_query(["ssl", "sslmode"])

    url = url.difference_update_query(_ASYNC_PG_STRIP_QUERY_KEYS)

    return url, {"connect_args": connect_args} if connect_args else {}


_database_url, _database_options = _build_database_engine_options()

# Create async engine
engine: AsyncEngine = create_async_engine(
    _database_url,
    echo=settings.debug,
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=20,
    **_database_options,
)

# Create async session factory
async_session_factory = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False,
)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """Get an async database session."""
    async with async_session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


@asynccontextmanager
async def get_session_context() -> AsyncGenerator[AsyncSession, None]:
    """Get a database session as a context manager."""
    async with async_session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def init_db() -> None:
    """Initialize database connection."""
    # Test connection
    async with engine.begin() as conn:
        await conn.run_sync(lambda _: None)


async def close_db() -> None:
    """Close database connection."""
    await engine.dispose()
