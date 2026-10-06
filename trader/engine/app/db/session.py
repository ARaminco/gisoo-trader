"""Async engine, session factory and schema bootstrap."""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine,
)

from app.config import settings
from app.db.models import Base

log = logging.getLogger(__name__)

# Columns added to existing tables after their first release. `create_all`
# only creates missing *tables*, and Alembic is not adopted yet
# (trader/engine/alembic/README.md), so every schema bootstrap adds these idempotently: a
# restart on an older database must not die with UndefinedColumn on the first
# SELECT. The DDL is rendered from the model, so the column here and in
# Postgres cannot drift apart. By hand, for an existing database:
#
#   ALTER TABLE equity_snapshots ADD COLUMN IF NOT EXISTS suspect BOOLEAN DEFAULT false NOT NULL;
#   ALTER TABLE exchange_sync ADD COLUMN IF NOT EXISTS balance_unexplained DOUBLE PRECISION DEFAULT 0 NOT NULL;
#   ALTER TABLE exchange_sync ADD COLUMN IF NOT EXISTS balance_unexplained_since TIMESTAMP WITH TIME ZONE;
#   ALTER TABLE positions ADD COLUMN IF NOT EXISTS pending_exit_reason VARCHAR(48);
#   ALTER TABLE positions ADD COLUMN IF NOT EXISTS pending_exit_price DOUBLE PRECISION;
#   ALTER TABLE positions ADD COLUMN IF NOT EXISTS pending_exit_at TIMESTAMP WITH TIME ZONE;
#   ALTER TABLE positions ADD COLUMN IF NOT EXISTS stop_missing BOOLEAN DEFAULT false NOT NULL;
#   ALTER TABLE positions ADD COLUMN IF NOT EXISTS stop_retry_at TIMESTAMP WITH TIME ZONE;
#   ALTER TABLE positions ADD COLUMN IF NOT EXISTS exit_check_bar BIGINT;
ADDITIVE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("equity_snapshots", "suspect"),
    ("exchange_sync", "balance_unexplained"),
    ("exchange_sync", "balance_unexplained_since"),
    ("positions", "pending_exit_reason"),
    ("positions", "pending_exit_price"),
    ("positions", "pending_exit_at"),
    ("positions", "stop_missing"),
    ("positions", "stop_retry_at"),
    ("positions", "exit_check_bar"),
)


def add_missing_columns(sync_conn) -> list[str]:
    """Add every ADDITIVE_COLUMNS entry the database lacks; returns what was added.

    Runs on a sync connection (`conn.run_sync`). Postgres gets `IF NOT EXISTS`
    so four workers bootstrapping at the same moment cannot trip each other.
    """
    inspector = inspect(sync_conn)
    tables = set(inspector.get_table_names())
    compiler = sync_conn.dialect.ddl_compiler(sync_conn.dialect, None)
    if_not_exists = "IF NOT EXISTS " if sync_conn.dialect.name == "postgresql" else ""
    added: list[str] = []
    for table_name, column_name in ADDITIVE_COLUMNS:
        if table_name not in tables:
            continue  # create_all just made it, with every column
        if column_name in {c["name"] for c in inspector.get_columns(table_name)}:
            continue
        column = Base.metadata.tables[table_name].columns[column_name]
        spec = compiler.get_column_specification(column)
        sync_conn.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {if_not_exists}{spec}"))
        added.append(f"{table_name}.{column_name}")
    return added

_engine: AsyncEngine | None = None
_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        _engine = create_async_engine(
            settings.database_url,
            pool_size=5,
            max_overflow=10,
            pool_pre_ping=True,
            pool_recycle=1800,
            echo=False,
        )
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _factory
    if _factory is None:
        _factory = async_sessionmaker(
            get_engine(), expire_on_commit=False, class_=AsyncSession
        )
    return _factory


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Transactional session. Commits on success, rolls back on error."""
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def init_db(retries: int = 30, delay: float = 2.0) -> None:
    """Create tables. Waits for Postgres to accept connections first."""
    engine = get_engine()
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
                added = await conn.run_sync(add_missing_columns)
            if added:
                log.info("added columns to existing tables: %s", ", ".join(added))
            log.info("database schema ready")
            return
        except Exception as exc:  # noqa: BLE001 - retry any connection failure
            last_error = exc
            log.warning("database not ready (attempt %s/%s): %s", attempt, retries, exc)
            await asyncio.sleep(delay)
    raise RuntimeError(f"could not initialise database: {last_error}")


async def dispose_db() -> None:
    global _engine, _factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _factory = None
