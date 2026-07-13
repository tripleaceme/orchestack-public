# Smoke — OpenMetadata first boot

Verifies the platform's longest cross-component dependency chain:
pre-start hook provisions the role and database, the Elasticsearch
sidecar reaches healthy, the OpenMetadata server completes its
first-boot Liquibase migrations, the post-start hook resets the
admin password AND applies the Elasticsearch replica-count fix, the
Traefik redirect from `/app/openmetadata` sends the operator to
port 8585, and the operator can sign into the OpenMetadata UI.

Called out in §4.5 of the project report as the most demanding smoke
because every link in the chain must succeed for the flow to complete.

**Applies to:** v0.1.1
**Duration:** ~10 min on first boot (image pull + Liquibase migrations
dominate); ~90 s on subsequent boots.

## Setup

- OrcheStack is installed and reachable per
  [`install-path.md`](install-path.md).
- Signed in to the dashboard at http://localhost/app as an operator
  whose role grants `can_start` on the openmetadata service.
- OpenMetadata has been added to the catalogue via the setup wizard
  (its row exists in `platform.installed_services` with
  `enabled = true`).
- OpenMetadata is currently STOPPED (its tile on the dashboard shows
  the grey "Stopped" state; no `orchestack-openmetadata*` containers
  are running).

## Steps

1. **From the dashboard's Services page, click the OpenMetadata tile's
   `Start` button.** Expected: the tile transitions to a `Starting…`
   state within one second (the optimistic UI update); a background
   task begins pulling images if the cache is cold.

2. **Watch `docker compose ps` in a terminal.** Expected: three
   containers come up in this order —
   `orchestack-openmetadata-es` (Elasticsearch),
   `orchestack-openmetadata-ingestion` (Airflow-based ingestion sidecar),
   `orchestack-openmetadata` (the OpenMetadata server). ES reaches
   `healthy` first, then the server. First-boot Liquibase migrations
   run inside the server container and take 90 to 120 seconds.

3. **On the dashboard, wait for the tile to flip from `Starting…` to
   `Running`.** Expected: the transition happens within ~2 minutes on
   first boot, ~30 seconds on subsequent boots. The tile shows
   `orchestack-openmetadata` as its container name.

4. **Click the tile's `Open` button.** Expected: a new browser tab
   opens at `http://localhost:8585` (NOT `/app/openmetadata` — the
   Traefik redirect described in §4.2.3 sends the operator to the
   direct host port because OpenMetadata's React bundle emits
   absolute-root asset paths that fail under a subpath proxy).

5. **Sign into OpenMetadata with the credentials the post-start hook
   set.** Username: `admin@open-metadata.org` (hardcoded by the upstream
   image). Password: the value of `OPENMETADATA_ADMIN_PASSWORD` from
   the operator's `.env`. Expected: OpenMetadata's landing page loads;
   the "no data yet" empty state is normal for a first boot.

6. **In OpenMetadata's UI, search for anything (e.g. type "a" into the
   search bar).** Expected: the search returns without an
   Elasticsearch error. If it returns "Elasticsearch is unhealthy
   (yellow)", the replica-count fix from the post-start hook has not
   applied — see verification query 2.

## Verification

### 1. Confirm the pre-start hook provisioned the database and role

```bash
docker exec orchestack-postgres \
  psql -U orchestack_admin -d orchestack_db \
  -c "SELECT rolname FROM pg_roles WHERE rolname = 'openmetadata_admin';"
docker exec orchestack-postgres \
  psql -U orchestack_admin -d orchestack_db \
  -c "SELECT datname FROM pg_database WHERE datname = 'openmetadata_db';"
```
Expected: one row from each — the role and the database.

### 2. Confirm the post-start hook applied the ES replica fix

```bash
docker exec orchestack-openmetadata-es \
  curl -s http://localhost:9200/_cluster/health | jq .status
```
Expected: `"green"`. `"yellow"` means the replica-count override did
not apply — a known-good v0.1.1 install always returns green after
the post-start hook completes.

### 3. Confirm the audit trail recorded the full sequence

```sql
SELECT id, event_type, target, actor_username,
       details->>'trigger' AS trigger_source
FROM platform.audit_log
WHERE target = 'openmetadata'
   OR event_type LIKE 'session_%'
   AND details->>'service' = 'openmetadata'
ORDER BY id DESC
LIMIT 10;
```
Expected rows, most-recent first:
- `service_auto_pinned` with `trigger = 'session_open'`
  (the 4-hour cold-tier auto-pin — described in the report's §4.2.3
  correction)
- `session_autostart` with `returncode = 0`
- `session_opened`
- Earlier: any prior stops for openmetadata

### 4. Confirm the OpenMetadata session record exists

```sql
SELECT id, service_name, user_id, opened_at, last_seen_at, cascade
FROM platform.service_sessions
WHERE service_name = 'openmetadata'
  AND closed_at IS NULL
ORDER BY opened_at DESC;
```
Expected: exactly one row for the operator's session, with `cascade =
false` (the operator opened it directly, not via a dependency).

## Known caveats

- First boot's Liquibase migration phase is the longest single wait in
  the entire platform. The dashboard's optimistic UI + toast notification
  ("starting OpenMetadata; this can take up to 2 minutes on first
  boot") is the mitigation but does not eliminate the wait — Section
  4.8 of the report discusses this trade-off explicitly.
- The 4-hour auto-pin applies to every cold-tier service (see the
  §4.2.3 correction), not just Airbyte as an earlier report draft
  claimed. OpenMetadata is included.
- Elasticsearch's `docker exec` health probe requires `curl` and `jq`
  inside the ES container — the upstream image ships both, but a
  custom ES image might not.
