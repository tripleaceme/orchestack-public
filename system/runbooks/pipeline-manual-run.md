# Smoke — Pipeline create + manual run

Verifies the lifecycle-pipelines feature added in v0.1.1. Creates a
pipeline through the dashboard editor, triggers it manually, watches
the runs page's step chain report per-step progress in real time, and
confirms the persisted step_results in the database match the DAG
the operator saw on screen.

This is the flow whose JSONB-codec defect (described in §4.4) blocked
the runs DAG for a full stabilisation-cycle day — the runs page
initially reported every step as `queued` regardless of the executor's
actual progress. This runbook is the reproducible baseline against
which any future regression on that surface is measured.

**Applies to:** v0.1.1
**Duration:** ~5 min (dominated by the buffer waits between steps).

## Setup

- OrcheStack is installed, reachable, and signed into as an operator
  whose role holds the `Admin` grant (only Admins can create pipelines).
- At least two cold-tier services are catalogued and configured. MinIO
  and dbt are the quickest to bring up and are used in the steps below.
- Both services are currently STOPPED (their tiles on the dashboard
  show `Stopped`; no `orchestack-minio` or `orchestack-dbt` containers
  are running).

## Steps

1. **Navigate to the Pipelines page.** Path: sidebar → Workspace →
   Pipelines. Expected: the page lists any existing pipelines and
   shows a `+ New pipeline` button in the top-right.

2. **Click `+ New pipeline`.** Expected: the pipeline editor opens
   with a Name field, an Enabled checkbox, a Trigger dropdown
   (default: `Manual only`), and an empty Steps section with an
   `+ Add a step` affordance.

3. **Set Name to `smoke-manual-minio-dbt` and leave Enabled ticked.**
   Trigger stays at `Manual only`.

4. **Click `+ Add a step` twice.** Expected: two rows appear, each
   with an order number, a service dropdown, an action dropdown
   (start/stop), a buffer input (default 300), and an `x` remove
   button. The dropdowns should include both cold-tier and hot-tier
   services; hot-tier ones show `(always-on)` after the display name.

5. **Configure step 1: service = MinIO, action = start, buffer = 15.**
   Configure step 2: service = dbt Core, action = start, buffer = 300
   (unused — last step's buffer is ignored per the docs).

6. **Click `Create`.** Expected: the page redirects to the Pipelines
   list and the new pipeline row appears with `manual` in the Trigger
   column and `-` in every other column (no runs yet).

7. **Click `View runs` on the new pipeline's row.** Expected: the runs
   page renders an empty Recent runs table and the horizontal step
   chain across the top shows both steps in `queued` state (blue
   pill), a `→` arrow with `buf 15s` between them.

8. **Click `Run now` (top-right of the runs page).** Expected: no
   confirmation popup; the first step's pill flips from blue to
   light green (`starting`) within one second — the optimistic UI
   update the report's §4.2.4 describes.

9. **Watch the DAG for ~20 seconds.** Expected transition sequence
   for step 1 (MinIO):
   - `starting` (light green, pulsing) for ~5-10 seconds while
     `docker compose up -d` completes on MinIO
   - `warming` (yellow) for ~15 seconds during the buffer wait
   - `running` (deep green) once step 2 begins

10. **Watch step 2 (dbt) transition.** Expected: same sequence —
    `queued` → `starting` → (skips `warming` because it's the last
    step) → `running`. Total pipeline duration ~30 seconds.

11. **Confirm the Recent runs row updated in-place.** Expected: the
    row shows the run's start time, `manual` as triggered_by,
    `succeeded` (deep green) as status, an end timestamp, and empty
    error_summary.

## Verification

### 1. Confirm both containers are actually running

```bash
docker compose ps
```
Expected: `orchestack-minio` and `orchestack-dbt` both `running` or
`healthy`. If the runs page showed `succeeded` but the containers are
not running, the executor is reporting success on failed docker calls
— a regression on the executor's exit-code handling.

### 2. Confirm the run's persisted state matches what the DAG showed

```sql
SELECT id, status, triggered_by, started_at, completed_at,
       jsonb_array_length(COALESCE(step_results, '[]'::jsonb)) AS step_count,
       jsonb_pretty(step_results) AS step_results
FROM platform.pipeline_runs
WHERE pipeline_id = (SELECT id FROM platform.pipelines
                     WHERE name = 'smoke-manual-minio-dbt')
ORDER BY id DESC LIMIT 1;
```
Expected: one row with `status = 'succeeded'`, `step_count = 2`, and
a step_results JSON array containing two objects — one for MinIO
(order_index 0) and one for dbt (order_index 1) — each with
`status = 'succeeded'` and non-null `started_at` + `completed_at`.

**Critically:** if `step_results` is a JSON-encoded STRING rather than
a JSON array (i.e. `"[{\"order_index\":0,..."` instead of
`[{"order_index":0,...`), the asyncpg JSONB codec fix from §4.4 has
regressed. This is the exact shape the pre-fix bug produced and the
reason every pill on the DAG showed `queued` before the fix landed.

### 3. Confirm the audit trail

```sql
SELECT id, event_type, target, actor_username,
       details->'run_id' AS run_id
FROM platform.audit_log
WHERE event_type LIKE 'pipeline_%'
ORDER BY id DESC LIMIT 5;
```
Expected rows, most-recent first:
- `pipeline_run_completed` with a run_id matching the run above
- `pipeline_run_started` with the same run_id
- `pipeline_created` for the initial creation

## Known caveats

- The 300-second buffer on step 2 is ignored because step 2 is the
  last step (documented behaviour — the report's §4.2.4 covers it).
  If a future runbook needs to verify the last-step-buffer skip
  explicitly, add a third dummy step and observe that step 2's buffer
  DOES fire.
- The runs page polls every 3 seconds while any run is in flight. If
  the operator has DevTools open with "Disable cache" enabled, each
  poll re-fetches the polling script — network traffic on the tab
  will be higher than in a normal session but functionally correct.
