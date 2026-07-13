#!/usr/bin/env bash
# Verify OrcheStack's container-level health-check layer end-to-end.
# Prints a matrix (declared healthcheck / container running / health
# state) and exits non-zero if anything is unhealthy — CI-friendly.
#
# See ./README.md for the report-mapped rationale for each check.

set -uo pipefail

# ── Colours (respect NO_COLOR / non-TTY) ──────────────────────────
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
  BOLD=$'\033[1m'; DIM=$'\033[2m'
  GREEN=$'\033[32m'; RED=$'\033[31m'; YELLOW=$'\033[33m'; CYAN=$'\033[36m'
  RESET=$'\033[0m'
else
  BOLD=""; DIM=""; GREEN=""; RED=""; YELLOW=""; CYAN=""; RESET=""
fi

info()  { printf '%s▸%s %s\n' "${CYAN}" "${RESET}" "$*"; }
ok()    { printf '  %s✓%s %s\n' "${GREEN}" "${RESET}" "$*"; }
warn()  { printf '  %s!%s %s\n' "${YELLOW}" "${RESET}" "$*"; }
fail()  { printf '  %s✗%s %s\n' "${RED}"    "${RESET}" "$*"; }

# ── Config ────────────────────────────────────────────────────────
REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
COMPOSE_DIR="${COMPOSE_DIR:-${REPO_ROOT}/system/docker}"
ORCHESTACK_URL="${ORCHESTACK_URL:-http://localhost}"

# Every service the platform ships. Kept in sync with SERVICE_CATALOGUE
# in the orchestrator's config.py; adding a new managed service means
# adding its compose file here.
CONTROL_PLANE=(
  "orchestack-postgres:${COMPOSE_DIR}/docker-compose.yml"
)
MANAGED=(
  "orchestack-metabase:${COMPOSE_DIR}/services/metabase.yml"
  "orchestack-pgadmin:${COMPOSE_DIR}/services/pgadmin.yml"
  "orchestack-minio:${COMPOSE_DIR}/services/minio.yml"
  "orchestack-dbt:${COMPOSE_DIR}/services/dbt.yml"
  "orchestack-ge:${COMPOSE_DIR}/services/ge.yml"
  "orchestack-airflow:${COMPOSE_DIR}/services/airflow.yml"
  "orchestack-airbyte:${COMPOSE_DIR}/services/airbyte.yml"
  "orchestack-openmetadata:${COMPOSE_DIR}/services/openmetadata.yml"
)

EXIT_CODE=0

# ── 1. Compose declarations — every service must define a HEALTHCHECK
info "${BOLD}Layer 1a — HEALTHCHECK directives declared in compose${RESET}"
for pair in "${CONTROL_PLANE[@]}" "${MANAGED[@]}"; do
  name="${pair%%:*}"; file="${pair##*:}"
  if [ ! -f "$file" ]; then
    warn "${name}: compose file not found at ${file} (skipping)"
    continue
  fi
  if grep -qE '^[[:space:]]+healthcheck:' "$file"; then
    ok "${name}  — healthcheck declared in $(basename "$file")"
  else
    fail "${name}  — NO healthcheck directive in $(basename "$file")"
    EXIT_CODE=1
  fi
done
echo

# ── 2. Runtime health state — Docker's own view of each container ─
info "${BOLD}Layer 1b — Runtime health state from Docker${RESET}"
if ! command -v docker >/dev/null 2>&1; then
  fail "docker CLI not found on PATH — cannot check runtime state"
  exit 2
fi

# Take a snapshot of every running orchestack-* container the daemon
# knows about. `mapfile` is bash 4+ so we avoid it — macOS ships bash
# 3.2 by default. A newline-joined string plus grep does the same job
# portably.
RUNNING_ORCHESTACK=$(docker ps --format '{{.Names}}' 2>/dev/null | grep '^orchestack-' || true)

for pair in "${CONTROL_PLANE[@]}" "${MANAGED[@]}"; do
  name="${pair%%:*}"
  is_running=false
  if printf '%s\n' "$RUNNING_ORCHESTACK" | grep -qxF "$name"; then
    is_running=true
  fi
  if [ "$is_running" = false ]; then
    printf '  %s-%s %s  — stopped (skipping health probe)\n' "${DIM}" "${RESET}" "${name}"
    continue
  fi
  # `.State.Health.Status` returns "healthy" / "unhealthy" / "starting"
  # for containers with a healthcheck; empty string when the container
  # was defined WITHOUT one (which would also make Layer 1a fail).
  status=$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' \
    "$name" 2>/dev/null || echo "inspect-failed")
  case "$status" in
    healthy)
      ok "${name}  — healthy"
      ;;
    starting)
      warn "${name}  — starting (health probe not yet green; run again in 30s)"
      ;;
    unhealthy)
      fail "${name}  — UNHEALTHY (check: docker logs ${name})"
      EXIT_CODE=1
      ;;
    none)
      warn "${name}  — running but no healthcheck attached; container has no state.Health"
      ;;
    *)
      fail "${name}  — inspect returned '${status}'"
      EXIT_CODE=1
      ;;
  esac
done
echo

# ── 3. Orchestrator control-plane /api/health ─────────────────────
info "${BOLD}Layer 1c — Orchestrator /api/health${RESET}"
if ! command -v curl >/dev/null 2>&1; then
  fail "curl not found on PATH — cannot query ${ORCHESTACK_URL}/app/api/health"
  EXIT_CODE=1
else
  # The endpoint returns JSON of shape:
  #   {"ok": true, "checks": {"postgres": true, "docker": true}, ...}
  # We check "ok" as the summary flag and any false in "checks".
  response=$(curl -fsS --max-time 5 "${ORCHESTACK_URL}/orchestrator/api/health" 2>/dev/null || echo "")
  if [ -z "$response" ]; then
    # Fallback: the orchestrator is often internal-only; try via the
    # dashboard's proxy path.
    response=$(curl -fsS --max-time 5 "${ORCHESTACK_URL}/app/api/dashboard/health" 2>/dev/null || echo "")
  fi
  if [ -z "$response" ]; then
    fail "orchestrator /api/health did not respond (checked ${ORCHESTACK_URL})"
    EXIT_CODE=1
  else
    # Pretty-print without needing jq: extract known fields with sed.
    ok_flag=$(printf '%s' "$response" | sed -nE 's/.*"ok"[[:space:]]*:[[:space:]]*(true|false).*/\1/p')
    postgres_flag=$(printf '%s' "$response" | sed -nE 's/.*"postgres"[[:space:]]*:[[:space:]]*(true|false).*/\1/p')
    docker_flag=$(printf '%s' "$response" | sed -nE 's/.*"docker"[[:space:]]*:[[:space:]]*(true|false).*/\1/p')
    if [ "$ok_flag" = "true" ]; then
      ok "orchestrator: ok=true  postgres=${postgres_flag}  docker=${docker_flag}"
    else
      fail "orchestrator: ok=${ok_flag:-?}  postgres=${postgres_flag:-?}  docker=${docker_flag:-?}"
      fail "raw response:  $(printf '%s' "$response" | head -c 300)"
      EXIT_CODE=1
    fi
  fi
fi
echo

# ── Summary ───────────────────────────────────────────────────────
if [ "$EXIT_CODE" -eq 0 ]; then
  printf '%s%sAll health checks passed.%s\n' "${BOLD}" "${GREEN}" "${RESET}"
else
  printf '%s%sHealth verification found problems above. Exit code: %d%s\n' \
    "${BOLD}" "${RED}" "${EXIT_CODE}" "${RESET}"
fi
exit "$EXIT_CODE"
