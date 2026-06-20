"""Async asyncpg connection pool against the OrcheStack internal DB."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

import asyncpg

from . import config

log = logging.getLogger("orchestrator.db")

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
        # Set search_path so queries don't need to qualify `platform.x`.
        server_settings={"search_path": "platform, public"},
    )
    log.info("postgres pool ready")
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("db pool not initialised — call init_pool() first")
    return _pool


async def fetch(query: str, *args: Any) -> list[asyncpg.Record]:
    async with get_pool().acquire() as conn:
        return await conn.fetch(query, *args)


async def fetchrow(query: str, *args: Any) -> asyncpg.Record | None:
    async with get_pool().acquire() as conn:
        return await conn.fetchrow(query, *args)


async def fetchval(query: str, *args: Any) -> Any:
    async with get_pool().acquire() as conn:
        return await conn.fetchval(query, *args)


async def execute(query: str, *args: Any) -> str:
    async with get_pool().acquire() as conn:
        return await conn.execute(query, *args)


@asynccontextmanager
async def transaction() -> AsyncIterator[asyncpg.Connection]:
    async with get_pool().acquire() as conn:
        async with conn.transaction():
            yield conn


async def ping() -> bool:
    try:
        await fetchval("SELECT 1")
        return True
    except Exception as e:
        log.warning("postgres ping failed: %s", e)
        return False
