# Smoke — install path

Verifies the operator's install path from a curl one-liner through to
a reachable dashboard. This is the flow every tester runs first; if it
fails, no other runbook is worth attempting until this one is green.

**Applies to:** v0.1.1
**Duration:** ~2 min on a warm image cache, ~5 min on first pull.

## Setup

- Host has Docker running (`docker info` succeeds).
- Ports 80 and 443 are free on the host.
- No previous OrcheStack install in the working directory (`ls ./orchestack`
  returns nothing).
- Working directory has at least 3 GB free on the same filesystem the
  Docker daemon uses for volumes.
- No `~/.orchestack` or global config that could shadow the install.

## Steps

1. **Run the one-liner installer:**
   ```bash
   curl -fsSL https://orchestack.pages.dev/install.sh | bash
   ```
   Expected: the script prints `▸ OrcheStack installer (latest)`
   followed by seven `✓`-prefixed lines confirming Host OS, Docker,
   Compose, daemon, downloaded bundle, checksum verified, extracted.

2. **Enter a password when prompted twice.** Anything ≥12 characters.
   Expected: the second prompt (Confirm) accepts the match and prints
   `✓ Wrote .env (chmod 600)`. If it loops on "Passwords don't match",
   the `read </dev/tty` fix from the July 12 commit is missing —
   abort and re-check the shipped install.sh.

3. **Wait for `docker compose up -d` to complete.** Expected: a
   `[+] Running 6/6` block showing the six control-plane containers
   (`orchestack-postgres`, `orchestack-auth`, `orchestack-socket-proxy`,
   `orchestack-proxy`, `orchestack-orchestrator`, `orchestack-dashboard`)
   all as either `Started` or `Healthy`.

4. **Wait for the "Waiting for the platform to come online..." poll
   to complete.** Expected: `✓ Auth container is responding.` within
   60 seconds of compose returning.

5. **Confirm the ASCII banner + status summary print.** Expected:
   the cyan OrcheStack banner followed by a green `OrcheStack v0.1.1
   is up.` line, then `✓ Health 6/6 containers healthy · started
   in Ns`, `✓ Location /absolute/path/to/orchestack`, and the
   next-step + useful-commands + Docs / Issues links.

6. **Open a browser to http://localhost.** Expected: the OrcheStack
   signup form loads (no users exist yet, so signup is the default).

## Verification

Verify each container's runtime state:

```bash
docker compose ps
```
Expected: 6 rows, all with `STATUS` containing `healthy` or `running`
and no `Restarting` or `Exit` states.

Verify the platform database was initialised:

```bash
docker exec orchestack-postgres \
  psql -U orchestack_admin -d orchestack_db \
  -c "\dt platform.*"
```
Expected: the tables listed in the schema migrations exist —
`users`, `roles`, `role_permissions`, `user_roles`, `sessions`,
`service_sessions`, `service_pinning`, `audit_log`, `applied_migrations`,
`installed_services`, `pipelines`, `pipeline_steps`, `pipeline_runs`.

Verify the audit log recorded the first-boot events:

```sql
SELECT id, event_type, created_at
FROM platform.audit_log
ORDER BY id ASC
LIMIT 10;
```
Expected: rows for the migration runner's `migration_applied` events
(one per SQL file in `postgres-init/`), the orchestrator's
`orchestrator_ready` event, and — if any hot-tier service was
autostarted — a `service_autostart` event per hot-tier service.

## Known caveats

- On Apple silicon, the first `docker compose up -d` can take up to
  5 minutes because the Postgres, Traefik, and other base images pull
  their `linux/arm64` layers on first install. Subsequent installs
  reuse the cache and complete in seconds.
- If the operator has pointed `DOCKER_HOST` at a remote daemon,
  ports 80/443 must be free on THAT host, not the operator's local
  machine. The installer does not currently detect this mismatch.
