"""Alembic environment. Run through `l1-agent migrate`, which supplies the URL."""

from __future__ import annotations

import asyncio

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

from src.l1_agent.db.models import metadata

config = context.config


def _run(connection) -> None:
    context.configure(connection=connection, target_metadata=metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def _online() -> None:
    engine = create_async_engine(config.get_main_option("sqlalchemy.url"))
    try:
        async with engine.connect() as connection:
            await connection.run_sync(_run)
            await connection.commit()
    finally:
        await engine.dispose()


if context.is_offline_mode():
    raise RuntimeError("Offline migrations are not supported; connect as the migration role")
asyncio.run(_online())
