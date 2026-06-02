"""Database access — async connection pool against the OrcheStack internal DB.

The pool is created once at app startup (FastAPI lifespan) and reused for
every request and every reconciler tick. We use asyncpg directly (no ORM)
because the query surface is small, the schemas are stable, and an ORM
would obscure the SQL that's already documented in the report.

This module exposes a small set of helpers — `get_pool()` to fetch the
shared pool, `fetch()`/`fetchrow()`/`execute()` thin wrappers, and a
`transaction()` context manager. Routes import these directly; nothing
holds onto a connection across awaits except for the duration of a single
query.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

import asyncpg

from . import config

log = logging.getLogger("orchestrator.db")

# Module-level pool reference. Set by `init_pool()` at startup, cleared by
# `close_pool()` at shutdown. None outside that window.
_pool: asyncpg.Pool | None = None


async def init_pool() -> asyncpg.Pool:
    """Create the connection pool. Idempotent — safe to call twice."""
    global _pool
    if _pool is not None:
        return _pool
    log.info(
        "connecting to postgres host=%s port=%s db=%s user=%s pool=%d-%d",
        config.DB_HOST, config.DB_PORT, config.DB_NAME, config.DB_USER,
        config.DB_POOL_MIN, config.DB_POOL_MAX,
    )
    _pool = await asyncpg.create_pool(
        host=config.DB_HOST,
        port=config.DB_PORT,
        user=config.DB_USER,
        password=config.DB_PASSWORD,
        database=config.DB_NAME,
        min_size=config.DB_POOL_MIN,
        max_size=config.DB_POOL_MAX,
        # Set search_path on every connection so we don't have to qualify
        # `platform.x` in every query. Saves clutter; the schema is stable.
        server_settings={"search_path": "platform, public"},
    )
    log.info("postgres pool ready")
    return _pool


async def close_pool() -> None:
    """Cleanly close the pool on shutdown."""
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def get_pool() -> asyncpg.Pool:
    """Return the shared pool — raises if init hasn't run yet."""
    if _pool is None:
        raise RuntimeError("db pool not initialised — call init_pool() first")
    return _pool


# ---------- Thin query wrappers --------------------------------------------
# Wrappers exist so route handlers don't all have to manage acquire/release
# pairs. asyncpg's Pool already does the right thing — these are just
# shorthand.

async def fetch(query: str, *args: Any) -> list[asyncpg.Record]:
    """SELECT multiple rows. Returns a (possibly empty) list of Records."""
    async with get_pool().acquire() as conn:
        return await conn.fetch(query, *args)


async def fetchrow(query: str, *args: Any) -> asyncpg.Record | None:
    """SELECT a single row. Returns None if no match."""
    async with get_pool().acquire() as conn:
        return await conn.fetchrow(query, *args)


async def fetchval(query: str, *args: Any) -> Any:
    """SELECT a single column from a single row. Returns None if no match."""
    async with get_pool().acquire() as conn:
        return await conn.fetchval(query, *args)


async def execute(query: str, *args: Any) -> str:
    """INSERT/UPDATE/DELETE. Returns the row-count status string."""
    async with get_pool().acquire() as conn:
        return await conn.execute(query, *args)


@asynccontextmanager
async def transaction() -> AsyncIterator[asyncpg.Connection]:
    """Acquire a connection + open a transaction.

    Use when multiple writes need to be atomic — e.g. the wizard handoff
    inserts setup_state AND installed_services rows; if one fails, both
    should roll back.
    """
    async with get_pool().acquire() as conn:
        async with conn.transaction():
            yield conn


async def ping() -> bool:
    """Returns True if postgres is reachable. Used by /api/health."""
    try:
        await fetchval("SELECT 1")
        return True
    except Exception as e:
        log.warning("postgres ping failed: %s", e)
        return False
