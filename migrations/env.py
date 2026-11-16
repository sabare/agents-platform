import asyncio
import os

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import create_async_engine

from agents_platform import models  # noqa: F401 -- registers tables for autogenerate
from agents_platform.config import get_settings
from agents_platform.db import Base

target_metadata = Base.metadata
settings = get_settings()
migration_url = (
    os.getenv("DATABASE_URL") or settings.migration_database_url or settings.database_url
)


def run_migrations(connection):
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_online():
    engine = create_async_engine(migration_url, poolclass=pool.NullPool)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(run_migrations)
    finally:
        await engine.dispose()


if context.is_offline_mode():
    context.configure(
        url=migration_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()
else:
    asyncio.run(run_online())
