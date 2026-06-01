-- OrcheStack — Internal database initialisation.
--
-- Runs ONCE on first container creation. PostgreSQL's docker-entrypoint script
-- sources every .sql / .sh file in /docker-entrypoint-initdb.d/ in lexical
-- order before the database is opened for connections. File numbering
-- (00-, 10-, 20-, ...) gives us deterministic ordering and the ability to
-- insert new init steps without renaming existing ones.
--
-- This file (00-init.sql) creates the `platform` schema inside the OrcheStack
-- internal database (named by POSTGRES_DB, which compose maps from
-- ORCHESTACK_DB_NAME in .env — default 'orchestack'). It runs WITHOUT an
-- explicit \connect so the script targets whatever the operator named it.
--
-- The full platform.* table set is created in 10-platform-schema.sql.
--
-- NOTE: The customer PIPELINE database (with schemas raw, marts, airflow,
-- openmetadata) is NOT created here. That's a wizard-driven step the
-- orchestrator (M2) performs after the operator submits the setup wizard.
-- Keeping pipeline schemas out of this file maintains the two-database split:
-- OrcheStack metadata stays small and bootstrap-controlled; customer data
-- lives in a separately-named DB with its own user.

-- ============================================================================
-- OrcheStack platform schema
-- ============================================================================

CREATE SCHEMA IF NOT EXISTS platform;

COMMENT ON SCHEMA platform IS 'OrcheStack internal metadata: users, roles, sessions, audit, service lifecycle.';

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
VALUES ('00-init', 'Created OrcheStack internal schema: platform.');
