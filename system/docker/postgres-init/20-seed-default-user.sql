-- Seed a default administrator user.
--
-- Runs on first container creation, AFTER 10-platform-schema.sql has
-- created the users + roles tables. PostgreSQL's docker-entrypoint sources
-- init files in lexical order, so file numbering controls execution order:
--   00-init.sql      → schemas + bootstrap_log
--   10-platform-schema.sql → all 10 platform tables + indexes + triggers
--   20-seed-default-user.sql → this file (runs LAST)
--
-- Why this seed exists
-- --------------------
-- The OrcheStack signup form writes profile data to localStorage
-- only — it does NOT create a row in platform.users. But every operation
-- the orchestrator does (session open, service start, audit log write,
-- pin) has a FK constraint requiring a valid user_id. Without a seeded
-- user, the very first wizard handoff would fail with
--   "insert violates foreign key constraint ..."
--
-- The seed creates id=1 as a system-owned admin account. The orchestrator
-- uses this id as the default actor for any operation where a real user
-- isn't available — the fallback for background tasks (like the
-- reconciler's audit-log writes).
--
-- DO NOT log in as this account in production. The password hash below is
-- intentionally a value that bcrypt cannot have produced — an invalid hash
-- string — so password verification always fails. Login is blocked.

INSERT INTO platform.users (
  id, username, email, full_name, password_hash, is_active, onboarding_completed
) VALUES (
  1,
  'system',
  'system@orchestack.local',
  'OrcheStack system user',
  '$invalid$cannot-log-in$',   -- unparseable as bcrypt → all auth attempts fail
  TRUE,
  TRUE
)
ON CONFLICT (id) DO NOTHING;

-- The users table's id is a BIGSERIAL — postgres advances the sequence on
-- every INSERT. After explicitly inserting id=1, the sequence still points
-- at 1, so the NEXT real user signup would try id=1 again and conflict.
-- Bump the sequence past our seed.
SELECT setval(
  pg_get_serial_sequence('platform.users', 'id'),
  GREATEST(
    1,
    (SELECT COALESCE(MAX(id), 0) FROM platform.users)
  )
);

INSERT INTO platform.bootstrap_log (step, message)
VALUES ('20-seed-default-user', 'Seeded system user id=1 (login disabled).');
