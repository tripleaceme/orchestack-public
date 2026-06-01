-- OrcheStack — Platform metadata schema.
--
-- Runs once on first container creation, after 00-init.sql has created the
-- schemas. Defines the 10 tables the orchestrator (M2), the Streamlit
-- dashboard (M3), and the in-package auth pages will read and write.
--
-- Design conventions used throughout this file:
--   - BIGSERIAL primary keys (simpler than UUIDs for single-host deployment).
--   - TIMESTAMPTZ for every timestamp (always stored in UTC).
--   - CHECK constraints where business rules can be enforced at schema level.
--   - Triggers for updated_at (one shared function, attached per-table).
--   - Indexes on FK columns and any column we expect to filter by frequently.
--   - Comments on every table; comments on columns whose purpose isn't obvious.
--   - Seed data for built-in Admin / Engineer / Analyst roles, idempotent via
--     ON CONFLICT DO NOTHING (so re-running the init is safe).

-- The docker-entrypoint runs init scripts against $ORCHESTACK_DB_NAME. 
-- This schema is designed around the assumption that $ORCHESTACK_DB_NAME
-- is a dedicated database for OrcheStack's internal use, and that customer pipelines connect
-- to separate databases. If you change this assumption, you'll need to adjust the schema and
-- queries accordingly (e.g. by namespacing every table with "orchestrator_" or similar).

SET search_path = platform, public;

-- ============================================================================
-- Shared utility — updated_at trigger function.
-- ============================================================================
CREATE OR REPLACE FUNCTION platform.set_updated_at()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
  NEW.updated_at := now();
  RETURN NEW;
END;
$$;

COMMENT ON FUNCTION platform.set_updated_at() IS
  'Trigger fn — sets the row''s updated_at to now(). Attached BEFORE UPDATE to every table with an updated_at column.';

-- ============================================================================
-- 1. users — Platform user accounts.
-- ============================================================================
CREATE TABLE IF NOT EXISTS platform.users (
  id                    BIGSERIAL PRIMARY KEY,
  username              TEXT NOT NULL UNIQUE
                          CHECK (length(username) BETWEEN 3 AND 32
                                 AND username ~ '^[a-zA-Z0-9_.\-]+$'),
  email                 TEXT NOT NULL UNIQUE
                          CHECK (length(email) > 0 AND position('@' IN email) > 1),
  full_name             TEXT NOT NULL CHECK (length(full_name) > 0),
  password_hash         TEXT NOT NULL CHECK (length(password_hash) > 0),
  company_name          TEXT,
  onboarding_completed  BOOLEAN NOT NULL DEFAULT FALSE,
  is_active             BOOLEAN NOT NULL DEFAULT TRUE,
  last_login_at         TIMESTAMPTZ,
  created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TRIGGER trg_users_updated_at
  BEFORE UPDATE ON platform.users
  FOR EACH ROW EXECUTE FUNCTION platform.set_updated_at();
COMMENT ON TABLE platform.users IS
  'Platform user accounts. The first successful signup is auto-promoted to Admin by application logic (not enforced in schema).';
COMMENT ON COLUMN platform.users.password_hash IS
  'bcrypt hash produced by the application (streamlit-authenticator). Never stored as plaintext, never compared in SQL.';
COMMENT ON COLUMN platform.users.onboarding_completed IS
  'False until the user has finished the 4-step setup wizard. Route guards bounce /app users with onboarding_completed=false to /setup/welcome.';

-- ============================================================================
-- 2. sessions — Login sessions (cookie-backed, default 12h TTL).
-- ============================================================================
CREATE TABLE IF NOT EXISTS platform.sessions (
  id              BIGSERIAL PRIMARY KEY,
  token           UUID NOT NULL UNIQUE DEFAULT gen_random_uuid(),
  user_id         BIGINT NOT NULL REFERENCES platform.users(id) ON DELETE CASCADE,
  ip_address      INET,
  user_agent      TEXT,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  expires_at      TIMESTAMPTZ NOT NULL,
  last_seen_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  revoked_at      TIMESTAMPTZ,
  CONSTRAINT sessions_expires_after_created CHECK (expires_at > created_at)
);
CREATE INDEX IF NOT EXISTS idx_sessions_token         ON platform.sessions(token);
CREATE INDEX IF NOT EXISTS idx_sessions_user_id       ON platform.sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_sessions_expires_at    ON platform.sessions(expires_at);
COMMENT ON TABLE platform.sessions IS
  'Active login sessions. Application checks revoked_at IS NULL AND expires_at > now() on every authenticated request.';
COMMENT ON COLUMN platform.sessions.token IS
  'Opaque UUID set as the session cookie. Never use this as a primary key in queries — it should be indexed but treated as a credential.';

-- ============================================================================
-- 3. roles — Built-in + custom roles.
-- ============================================================================
CREATE TABLE IF NOT EXISTS platform.roles (
  id              BIGSERIAL PRIMARY KEY,
  name            TEXT NOT NULL UNIQUE CHECK (length(name) > 0),
  description     TEXT,
  is_system       BOOLEAN NOT NULL DEFAULT FALSE,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TRIGGER trg_roles_updated_at
  BEFORE UPDATE ON platform.roles
  FOR EACH ROW EXECUTE FUNCTION platform.set_updated_at();
COMMENT ON TABLE platform.roles IS
  'Named roles for RBAC. is_system=true for the built-in Admin/Engineer/Analyst trio; these cannot be deleted from the dashboard.';

-- ============================================================================
-- 4. user_roles — Many-to-many between users and roles.
-- ============================================================================
CREATE TABLE IF NOT EXISTS platform.user_roles (
  user_id           BIGINT NOT NULL REFERENCES platform.users(id) ON DELETE CASCADE,
  role_id           BIGINT NOT NULL REFERENCES platform.roles(id) ON DELETE CASCADE,
  granted_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  granted_by_user_id BIGINT REFERENCES platform.users(id) ON DELETE SET NULL,
  PRIMARY KEY (user_id, role_id)
);
CREATE INDEX IF NOT EXISTS idx_user_roles_role_id ON platform.user_roles(role_id);
COMMENT ON TABLE platform.user_roles IS
  'Which users have which roles. The first-signed-up user is auto-assigned the Admin role by application logic.';
COMMENT ON COLUMN platform.user_roles.granted_by_user_id IS
  'NULL for the first auto-assigned Admin and for system-bootstrapped grants; otherwise the admin who issued the grant.';

-- ============================================================================
-- 5. role_permissions — Per-role, per-service permission matrix.
--    Uses service_name = '*' as a wildcard meaning "applies to all services".
--    Application logic does:
--       SELECT ... WHERE role_id IN (user's roles)
--                    AND (service_name = ? OR service_name = '*')
--                    ORDER BY service_name = '*'   -- specific row wins
--       LIMIT 1
-- ============================================================================
CREATE TABLE IF NOT EXISTS platform.role_permissions (
  id                BIGSERIAL PRIMARY KEY,
  role_id           BIGINT NOT NULL REFERENCES platform.roles(id) ON DELETE CASCADE,
  service_name      TEXT NOT NULL CHECK (length(service_name) > 0),
  can_start         BOOLEAN NOT NULL DEFAULT FALSE,
  can_use           BOOLEAN NOT NULL DEFAULT FALSE,
  can_force_stop    BOOLEAN NOT NULL DEFAULT FALSE,
  can_edit_config   BOOLEAN NOT NULL DEFAULT FALSE,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (role_id, service_name)
);
CREATE TRIGGER trg_role_permissions_updated_at
  BEFORE UPDATE ON platform.role_permissions
  FOR EACH ROW EXECUTE FUNCTION platform.set_updated_at();
CREATE INDEX IF NOT EXISTS idx_role_permissions_service ON platform.role_permissions(service_name);
COMMENT ON TABLE platform.role_permissions IS
  'Per-role permission matrix. service_name=''*'' is a wildcard; specific service rows override the wildcard for that service.';
COMMENT ON COLUMN platform.role_permissions.can_force_stop IS
  'Permission to stop a service that still has active sessions from OTHER users. Always audited in platform.audit_log.';

-- ============================================================================
-- 6. installed_services — Registry of configured services on this install.
--    Populated by the setup wizard on completion; updated when an admin
--    adds/removes services via "Add services" in the dashboard.
-- ============================================================================
CREATE TABLE IF NOT EXISTS platform.installed_services (
  id                    BIGSERIAL PRIMARY KEY,
  name                  TEXT NOT NULL UNIQUE
                          CHECK (length(name) > 0 AND name = lower(name)),
  display_name          TEXT NOT NULL,
  layer                 TEXT NOT NULL
                          CHECK (layer IN ('ingestion','orchestration','warehouse','transformation',
                                           'data-lake','quality','governance','bi','admin-ui','control-plane')),
  tier                  TEXT NOT NULL CHECK (tier IN ('hot','cold')),
  idle_timeout_seconds  INTEGER NOT NULL DEFAULT 300 CHECK (idle_timeout_seconds >= 0),
  enabled               BOOLEAN NOT NULL DEFAULT TRUE,
  configured_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
  configured_by_user_id BIGINT REFERENCES platform.users(id) ON DELETE SET NULL,
  notes                 TEXT
);
CREATE INDEX IF NOT EXISTS idx_installed_services_enabled_tier
  ON platform.installed_services(enabled, tier);
COMMENT ON TABLE platform.installed_services IS
  'Services the operator has configured for this OrcheStack install. The orchestrator only manages services listed here.';
COMMENT ON COLUMN platform.installed_services.name IS
  'Lowercase short name matching the Docker Compose service name (e.g. ''airbyte'', ''dbt'', ''pgadmin'').';
COMMENT ON COLUMN platform.installed_services.idle_timeout_seconds IS
  'How long a cold-tier service may remain idle (zero active sessions) before the orchestrator stops it. Ignored for hot-tier services.';

-- ============================================================================
-- 7. service_sessions — Reference-counted active sessions.
--    A row is INSERTed when a user opens a cold-tier service; closed_at is
--    set when the user navigates away or the heartbeat times out. The
--    orchestrator counts WHERE closed_at IS NULL AND last_heartbeat_at >
--    (now() - interval '5 minutes') to decide whether a service is in use.
-- ============================================================================
CREATE TABLE IF NOT EXISTS platform.service_sessions (
  id                BIGSERIAL PRIMARY KEY,
  service_name      TEXT NOT NULL,
  user_id           BIGINT NOT NULL REFERENCES platform.users(id) ON DELETE CASCADE,
  opened_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_heartbeat_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  closed_at         TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_service_sessions_active
  ON platform.service_sessions(service_name)
  WHERE closed_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_service_sessions_user
  ON platform.service_sessions(user_id, closed_at);
COMMENT ON TABLE platform.service_sessions IS
  'Reference-counted active user sessions per service. The orchestrator''s idle-timeout sweep reads this to decide what to stop.';
COMMENT ON COLUMN platform.service_sessions.last_heartbeat_at IS
  'Streamlit sends a heartbeat every 30s while the user is on the service page. Sessions without a heartbeat for 5 minutes are treated as stale.';

-- ============================================================================
-- 8. service_pinning — "Keep warm" pins.
--    Suppresses the orchestrator's idle-timeout shutdown for a named service
--    until the pin expires or is rescinded. One row per pinned service.
-- ============================================================================
CREATE TABLE IF NOT EXISTS platform.service_pinning (
  id                BIGSERIAL PRIMARY KEY,
  service_name      TEXT NOT NULL UNIQUE,
  pinned_by_user_id BIGINT NOT NULL REFERENCES platform.users(id) ON DELETE CASCADE,
  pinned_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
  expires_at        TIMESTAMPTZ,
  reason            TEXT
);
CREATE INDEX IF NOT EXISTS idx_service_pinning_expires_at
  ON platform.service_pinning(expires_at)
  WHERE expires_at IS NOT NULL;
COMMENT ON TABLE platform.service_pinning IS
  'Per-service "Keep warm" pins. Orchestrator''s idle-timeout sweep skips services with a row here whose expires_at IS NULL OR expires_at > now().';
COMMENT ON COLUMN platform.service_pinning.expires_at IS
  'NULL = pin until manually rescinded. Otherwise the orchestrator cleanup deletes the row after expiry. Default: 2 hours from pinned_at (set by the dashboard).';

-- ============================================================================
-- 9. audit_log — Append-only record of privileged actions.
-- ============================================================================
CREATE TABLE IF NOT EXISTS platform.audit_log (
  id             BIGSERIAL PRIMARY KEY,
  event_type     TEXT NOT NULL CHECK (length(event_type) > 0),
  actor_user_id  BIGINT REFERENCES platform.users(id) ON DELETE SET NULL,
  target         TEXT,
  details        JSONB NOT NULL DEFAULT '{}'::jsonb,
  ip_address     INET,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_audit_log_created_at ON platform.audit_log(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_log_event_type ON platform.audit_log(event_type);
CREATE INDEX IF NOT EXISTS idx_audit_log_actor      ON platform.audit_log(actor_user_id);
COMMENT ON TABLE platform.audit_log IS
  'Append-only audit trail. Every privileged action (role change, force-stop, pin set/unset, credentials edit, RBAC change) writes one row here.';
COMMENT ON COLUMN platform.audit_log.actor_user_id IS
  'NULL for system-initiated events (e.g. orchestrator auto-stop). Otherwise the user who triggered the action.';
COMMENT ON COLUMN platform.audit_log.details IS
  'Free-form JSON for action-specific payload (e.g. {"old_role":"Engineer","new_role":"Admin"}).';

-- ============================================================================
-- 10. setup_state — In-progress setup wizard state per user.
--     Lets a user resume the 4-step wizard if they close the browser mid-flow.
--     Credentials are NOT stored here (they're written to ./config/.env at
--     deploy time); only the tool-selection picks.
-- ============================================================================
CREATE TABLE IF NOT EXISTS platform.setup_state (
  user_id        BIGINT PRIMARY KEY REFERENCES platform.users(id) ON DELETE CASCADE,
  current_step   TEXT NOT NULL DEFAULT 'welcome'
                   CHECK (current_step IN ('welcome','select','configure','deploying','completed')),
  selections     JSONB NOT NULL DEFAULT '{}'::jsonb,
  started_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  completed_at   TIMESTAMPTZ
);
CREATE TRIGGER trg_setup_state_updated_at
  BEFORE UPDATE ON platform.setup_state
  FOR EACH ROW EXECUTE FUNCTION platform.set_updated_at();
COMMENT ON TABLE platform.setup_state IS
  'Resumable state for the 4-step setup wizard. Per-user; cleared when current_step transitions to completed and onboarding_completed flips on users.';
COMMENT ON COLUMN platform.setup_state.selections IS
  'JSON of layer→tool choices, e.g. {"ingestion":"Airbyte","transformation":"dbt Core","warehouse":"PostgreSQL"}.';

-- ============================================================================
-- Seed data — built-in Admin / Engineer / Analyst roles.
-- ON CONFLICT DO NOTHING makes this idempotent if the init runs again.
-- ============================================================================

INSERT INTO platform.roles (name, description, is_system) VALUES
  ('Admin',
   'Full control: manages users, roles, service lifecycle, credentials, audit log review. The first signed-up user is auto-assigned this role.',
   TRUE),
  ('Engineer',
   'Data engineering: can start/stop services, use any tool, edit service credentials. Cannot force-stop services with other users'' active sessions, cannot manage users or roles.',
   TRUE),
  ('Analyst',
   'Read access: can use BI tools (Metabase) and run ad-hoc queries (pgAdmin), but cannot start/stop services or edit credentials.',
   TRUE)
ON CONFLICT (name) DO NOTHING;

-- Seed permissions for the built-in roles using the '*' wildcard (applies to
-- all services unless a more specific row is added later).
INSERT INTO platform.role_permissions (role_id, service_name, can_start, can_use, can_force_stop, can_edit_config)
SELECT id, '*', TRUE,  TRUE, TRUE,  TRUE  FROM platform.roles WHERE name = 'Admin'
UNION ALL
SELECT id, '*', TRUE,  TRUE, FALSE, TRUE  FROM platform.roles WHERE name = 'Engineer'
UNION ALL
SELECT id, '*', FALSE, TRUE, FALSE, FALSE FROM platform.roles WHERE name = 'Analyst'
ON CONFLICT (role_id, service_name) DO NOTHING;

-- ============================================================================
-- Bootstrap log entry — confirms this step ran.
-- ============================================================================
INSERT INTO platform.bootstrap_log (step, message)
VALUES ('10-platform-schema',
        'Created 10 platform.* tables, indexes, triggers, and seeded built-in Admin/Engineer/Analyst roles.');
