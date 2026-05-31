-- OrcheStack — Base PostgreSQL initialization.
--
-- Runs ONCE on first container creation. PostgreSQL's docker-entrypoint script
-- sources every .sql / .sh file in /docker-entrypoint-initdb.d/ in lexical
-- order before the database is opened for connections. File numbering
-- (00-, 10-, 20-, ...) gives us deterministic ordering and the ability to
-- insert new init steps without renaming existing ones.
--
-- This file (00-init.sql) creates the three top-level schemas.
-- The full platform.* table set is created in 10-platform-schema.sql at M1 step 1.3.

\connect orchestack

-- ============================================================================
-- Schemas
-- ============================================================================

CREATE SCHEMA IF NOT EXISTS platform;
CREATE SCHEMA IF NOT EXISTS raw;
CREATE SCHEMA IF NOT EXISTS marts;

COMMENT ON SCHEMA platform IS 'OrcheStack platform metadata: users, roles, sessions, audit, service lifecycle.';
COMMENT ON SCHEMA raw      IS 'Raw landed data from Airbyte ingestion (populated at M4).';
COMMENT ON SCHEMA marts    IS 'dbt-modelled analytical marts consumed by Metabase (populated at M4).';

-- ============================================================================
-- Bootstrap log — proves the init scripts ran. Useful smoke test before the
-- full schema lands. The orchestrator and Streamlit can read this to display
-- "Platform initialised at <timestamp>" in the dashboard header at M3.
-- ============================================================================

CREATE TABLE IF NOT EXISTS platform.bootstrap_log (
  id          SERIAL PRIMARY KEY,
  step        TEXT NOT NULL,
  message     TEXT NOT NULL,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE platform.bootstrap_log IS
  'Append-only log of PostgreSQL init steps. Each row records one init script firing on first boot.';

INSERT INTO platform.bootstrap_log (step, message)
VALUES ('00-init', 'Created schemas platform, raw, marts.');
