"""Pending-migration runner.

On orchestrator startup, walks /postgres-init/*.sql (the operator's
runtime bundle's migration files) in lexical order and applies each
one that hasn't been recorded in platform.applied_migrations.

Why this lives in the orchestrator rather than postgres's
docker-entrypoint:
  - docker-entrypoint only runs the files on FIRST init (empty data
    directory). Any migration shipped after the operator's initial
    install never gets applied to their existing data volume.
  - The operator was being asked to manually `psql < new-migration.sql`
    every upgrade. Easy to forget, breaks the platform in subtle ways
    (the dashboard's /pipelines page raised a 500 for one tester).

Backfill: on the very first orchestrator boot AFTER we ship this
runner, applied_migrations will be empty even though the existing
00/10/20 files ALREADY ran (via docker-entrypoint at first install).
We detect this — platform.users exists + applied_migrations is empty —
and backfill the pre-existing-file records so the runner doesn't try
to re-run them (which would explode on non-idempotent CREATE TRIGGER
statements in 10-platform-schema.sql).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from . import db

log = logging.getLogger(__name__)

# Mount point inside the orchestrator container. Wired up in
# system/docker/docker-compose.yml — `./postgres-init:/postgres-init:ro`.
MIGRATIONS_DIR = Path("/postgres-init")


_TRACKING_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS platform.applied_migrations (
    filename    TEXT PRIMARY KEY,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Used to detect "this file ran via docker-entrypoint at first
    -- install, not via the orchestrator's migration runner."
    source      TEXT NOT NULL DEFAULT 'orchestrator'
);
"""


async def apply_pending_migrations() -> None:
    """Idempotent: safe to call on every startup."""
    if not MIGRATIONS_DIR.exists():
        log.warning(
            "migrations: %s not mounted; skipping. "
            "(docker-compose.yml may be missing the postgres-init bind mount)",
            MIGRATIONS_DIR,
        )
        return

    # 1. Ensure tracking table. Uses IF NOT EXISTS so a re-run does nothing.
    try:
        await db.fetch(_TRACKING_TABLE_DDL)
    except Exception as e:
        log.error("migrations: could not ensure applied_migrations table: %s", e)
        return

    # 2. Detect "pre-existing install" — platform.users exists (from the
    #    docker-entrypoint-applied 10-platform-schema.sql) but our tracking
    #    table is empty. Backfill records for all currently-present .sql
    #    files in the mount, marked source='docker-entrypoint', so the
    #    runner below doesn't try to re-execute them.
    try:
        users_table_exists = bool(
            await db.fetchrow(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = 'platform' AND table_name = 'users'"
            )
        )
    except Exception as e:
        log.error("migrations: pre-existing-install detection failed: %s", e)
        return

    if users_table_exists:
        tracked_count = await db.fetchrow(
            "SELECT count(*) AS c FROM platform.applied_migrations"
        )
        if tracked_count and tracked_count["c"] == 0:
            # Backfill — for an existing install, assume every file
            # present in the mount predates this runner.
            log.info("migrations: backfilling docker-entrypoint-applied records")
            for sql_path in sorted(MIGRATIONS_DIR.glob("*.sql")):
                await db.fetch(
                    "INSERT INTO platform.applied_migrations (filename, source) "
                    "VALUES ($1, 'docker-entrypoint') ON CONFLICT (filename) DO NOTHING",
                    sql_path.name,
                )

    # 3. Walk the mount in lexical order; apply any file not yet recorded.
    applied = {
        r["filename"]
        for r in await db.fetch("SELECT filename FROM platform.applied_migrations")
    }
    sql_files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    for sql_path in sql_files:
        if sql_path.name in applied:
            continue
        log.info("migrations: applying %s", sql_path.name)
        try:
            sql = sql_path.read_text()
        except OSError as e:
            log.error("migrations: could not read %s: %s", sql_path, e)
            continue
        try:
            await db.fetch(sql)
        except Exception as e:
            log.error(
                "migrations: %s failed: %s. The migration runner stops here; "
                "subsequent migrations will not be attempted on this startup. "
                "Investigate, fix the SQL, restart the orchestrator.",
                sql_path.name, e,
            )
            # Stop on first failure — later migrations may depend on the
            # failed one, and partially applying creates harder-to-debug
            # state than just bailing.
            return
        await db.fetch(
            "INSERT INTO platform.applied_migrations (filename, source) "
            "VALUES ($1, 'orchestrator') ON CONFLICT (filename) DO NOTHING",
            sql_path.name,
        )
        log.info("migrations: %s applied", sql_path.name)
