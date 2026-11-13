from collections.abc import AsyncIterator

from fastapi import Request
from sqlalchemy import MetaData, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    # Import future model modules in migrations/env.py so autogenerate sees them.
    metadata = MetaData(
        naming_convention={
            "ix": "ix_%(column_0_label)s",
            "uq": "uq_%(table_name)s_%(column_0_N_name)s",
            "ck": "ck_%(table_name)s_%(constraint_name)s",
            "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
            "pk": "pk_%(table_name)s",
        }
    )


class Database:
    def __init__(self, url: str) -> None:
        self.engine = create_async_engine(
            url, pool_pre_ping=True, connect_args={"connect_timeout": 3}
        )
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    async def check(self) -> str:
        """Verify connectivity and the migration-installed vector extension."""
        async with self.engine.connect() as connection:
            version = await connection.scalar(
                text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
            )
            if version is None:
                raise RuntimeError("pgvector is missing; run alembic upgrade head")
            return str(version)

    async def close(self) -> None:
        await self.engine.dispose()


async def session_dependency(request: Request) -> AsyncIterator[AsyncSession]:
    """One session per request. Services explicitly commit their own transactions."""
    async with request.app.state.database.sessions() as session:
        yield session
