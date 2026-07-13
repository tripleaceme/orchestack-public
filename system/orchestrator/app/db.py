"""Async asyncpg connection pool against the OrcheStack internal DB."""

from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

import asyncpg

from . import config

log = logging.getLogger("orchestrator.db")

_pool: asyncpg.Pool | None = None


async def _init_connection(conn: asyncpg.Connection) -> None:
    """Per-connection init: register a JSONB codec so JSONB columns
    surface in Python as dicts/lists instead of raw JSON strings.

    Without this codec, asyncpg returns JSONB as a string. The pipeline
    runs page was the surfaced symptom: step_results came back as a
    JSON-encoded string, Jinja's selectattr couldn't iterate it, every
    step rendered as "queued" even after several had succeeded. Fixing
    it at the pool layer covers every JSONB column in the schema
    (audit_log.details, etc.) so the same class of bug can't recur.
    """
    await conn.set_type_codec(
        "jsonb",
        encoder=json.dumps,
        decoder=json.loads,
        schema="pg_catalog",
    )
    await conn.set_type_codec(
        "json",
        encoder=json.dumps,
        decoder=json.loads,
        schema="pg_catalog",
    )


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
        # Per-connection init: register JSON/JSONB codecs.
        init=_init_connection,
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
    """/api/health readiness check for Postgres.

    Lazy-inits the pool if it is None so the orchestrator recovers
    from a first-boot init_pool() failure once Postgres becomes
    reachable — e.g. after an operator rotates a bad password or
    restarts the postgres container. Without this lazy retry, a
    single startup-time auth failure permanently degrades the
    orchestrator until it is itself restarted, which surprised the
    v0.1.1 stabilisation cycle.
    """
    global _pool
    if _pool is None:
        try:
            await init_pool()
        except Exception as e:
            log.warning("postgres ping: lazy init_pool failed: %s", e)
            return False
    try:
        await fetchval("SELECT 1")
        return True
    except Exception as e:
        log.warning("postgres ping failed: %s", e)
        return False
