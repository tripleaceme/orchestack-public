-- ============================================================================
-- OrcheStack pipelines schema (v0.1.1)
-- ============================================================================
-- Lifecycle-only pipeline scheduling: an operator chains a sequence of
-- service starts on a trigger (manual / one-shot time / cron). The
-- orchestrator's scheduler loop fires each pipeline at its trigger
-- time and start_service()s each step's service in order, with a
-- per-step buffer between starts. The pipeline does NOT run jobs —
-- it just guarantees the services are up at the right time so their
-- own internal triggers (Airbyte connection schedules, Airflow DAG
-- schedules, dbt-cosmos DAGs, etc.) can fire.
--
-- Why this lives in the orchestrator and not Airflow: Airflow itself
-- is cold-tier and can't bootstrap itself. The orchestrator runs in
-- the always-on control plane so it can wake any cold-tier service
-- (including Airflow) before its scheduled work.
-- ============================================================================

CREATE TABLE IF NOT EXISTS platform.pipelines (
    id                  BIGSERIAL PRIMARY KEY,
    name                TEXT NOT NULL UNIQUE,
    description         TEXT,

    -- 'manual': only fires when operator clicks Run.
    -- 'once':   fires once at trigger_value (ISO timestamp), then becomes 'manual'.
    -- 'cron':   fires recurringly on trigger_value (cron expression).
    trigger_type        TEXT NOT NULL CHECK (trigger_type IN ('manual', 'once', 'cron')),

    -- For 'once': RFC-3339 timestamp string.
    -- For 'cron': 5-field cron expression (e.g. '0 8 * * *').
    -- For 'manual': NULL.
    trigger_value       TEXT,

    -- IANA timezone for cron evaluation. Default UTC so a half-configured
    -- pipeline still has predictable semantics.
    trigger_timezone    TEXT NOT NULL DEFAULT 'UTC',

    -- Disabled pipelines stay in the table (for history) but never fire.
    enabled             BOOLEAN NOT NULL DEFAULT TRUE,

    -- Cached next-run computed by the scheduler on every fire so the UI
    -- can show "next run: 8:00 AM tomorrow" without parsing cron client-side.
    next_run_at         TIMESTAMPTZ,
    last_run_at         TIMESTAMPTZ,
    last_run_status     TEXT,   -- 'succeeded' / 'failed' / 'running'

    created_by_user_id  BIGINT REFERENCES platform.users(id) ON DELETE SET NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS pipelines_enabled_trigger_idx
    ON platform.pipelines (enabled, trigger_type, next_run_at);


CREATE TABLE IF NOT EXISTS platform.pipeline_steps (
    id              BIGSERIAL PRIMARY KEY,
    pipeline_id     BIGINT NOT NULL REFERENCES platform.pipelines(id) ON DELETE CASCADE,

    -- 0-indexed position. Steps execute in ascending order.
    order_index     INT NOT NULL,

    -- Service catalogue key — 'airbyte', 'dbt', 'airflow', etc.
    -- Not a foreign key because the catalogue lives in the orchestrator
    -- Python, not in the database. Validation happens at API layer.
    service_name    TEXT NOT NULL,

    -- 'start' brings the service container up via docker_ops.start_service.
    -- 'stop' tears it down. Pipelines that need both can list two steps.
    action          TEXT NOT NULL DEFAULT 'start' CHECK (action IN ('start', 'stop')),

    -- Wait this many seconds AFTER the action completes (or fails) before
    -- the executor moves to the next step. Gives the service time to
    -- finish its own internal startup (Airflow scheduler tick, Airbyte
    -- worker register, etc.). Default 300s = 5 min based on operator
    -- input that 5-10 min is a reasonable buffer.
    buffer_seconds  INT NOT NULL DEFAULT 300,

    UNIQUE (pipeline_id, order_index)
);

CREATE INDEX IF NOT EXISTS pipeline_steps_pipeline_idx
    ON platform.pipeline_steps (pipeline_id, order_index);


CREATE TABLE IF NOT EXISTS platform.pipeline_runs (
    id                  BIGSERIAL PRIMARY KEY,
    pipeline_id         BIGINT NOT NULL REFERENCES platform.pipelines(id) ON DELETE CASCADE,

    -- 'manual', 'once', 'cron' — copied from the pipeline at fire time so
    -- the history survives even if the pipeline's trigger config changes
    -- later.
    triggered_by        TEXT NOT NULL,
    triggered_by_user_id BIGINT REFERENCES platform.users(id) ON DELETE SET NULL,

    -- 'running' while in flight; flips to 'succeeded' or 'failed' on completion.
    status              TEXT NOT NULL DEFAULT 'running'
                            CHECK (status IN ('running', 'succeeded', 'failed', 'cancelled')),

    started_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at        TIMESTAMPTZ,

    -- JSON array of step results — same order as pipeline_steps.order_index.
    -- Each entry: {service_name, action, status, error?, started_at, completed_at}
    step_results        JSONB,

    -- Surfaces "the pipeline ran but step 2 failed" without forcing a
    -- step_results parse.
    error_summary       TEXT
);

CREATE INDEX IF NOT EXISTS pipeline_runs_pipeline_idx
    ON platform.pipeline_runs (pipeline_id, started_at DESC);


-- Reuse the existing platform.updated_at trigger pattern.
CREATE OR REPLACE FUNCTION platform.pipelines_touch_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS pipelines_touch_updated_at ON platform.pipelines;
CREATE TRIGGER pipelines_touch_updated_at
    BEFORE UPDATE ON platform.pipelines
    FOR EACH ROW EXECUTE FUNCTION platform.pipelines_touch_updated_at();


-- Bootstrap log entry so an operator can confirm this migration ran.
INSERT INTO platform.bootstrap_log (step, message)
VALUES ('30-pipelines-schema',
        'Created platform.pipelines, pipeline_steps, pipeline_runs.')
ON CONFLICT DO NOTHING;
