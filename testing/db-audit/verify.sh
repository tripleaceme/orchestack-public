#!/usr/bin/env bash
# Verify OrcheStack's persisted-state invariants end-to-end.
# Reads via `docker exec orchestack-postgres psql` — no host psql
# needed, no credentials on the CLI. Read-only.
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
POSTGRES_CONTAINER="${POSTGRES_CONTAINER:-orchestack-postgres}"
POSTGRES_DB="${POSTGRES_DB:-orchestack_db}"

EXIT_CODE=0

# psql wrapper: -tA gives tuples-only, aligned-off output so query
# results can be counted directly with wc -l or piped into read.
q() {
  local sql=$1
  docker exec -i "$POSTGRES_CONTAINER" \
    sh -c "psql -tA -U \"\$POSTGRES_USER\" -d \"$POSTGRES_DB\"" <<< "$sql" 2>&1
}

# ── Pre-flight: container reachable? ──────────────────────────────
if ! docker inspect "$POSTGRES_CONTAINER" >/dev/null 2>&1; then
  fail "container '${POSTGRES_CONTAINER}' does not exist on this host"
  exit 2
fi
if ! docker exec "$POSTGRES_CONTAINER" pg_isready -q 2>/dev/null; then
  fail "container '${POSTGRES_CONTAINER}' is not accepting connections"
  exit 2
fi

# ── 1. Schema completeness ─────────────────────────────────────────
info "${BOLD}Layer 4a — Schema completeness${RESET}"
EXPECTED_TABLES="users roles role_permissions user_roles sessions service_sessions service_pinning audit_log installed_services applied_migrations pipelines pipeline_steps pipeline_runs"
missing=""
for t in $EXPECTED_TABLES; do
  present=$(q "SELECT to_regclass('platform.${t}') IS NOT NULL;" | tr -d '[:space:]')
  if [ "$present" = "t" ]; then
    printf '  %s✓%s %splatform.%s%s\n' "${GREEN}" "${RESET}" "${DIM}" "$t" "${RESET}"
  else
    fail "platform.${t}  — MISSING"
    missing="$missing $t"
    EXIT_CODE=1
  fi
done
if [ -z "$missing" ]; then
  ok "All ${BOLD}$(echo $EXPECTED_TABLES | wc -w | tr -d ' ')${RESET} expected tables present"
fi
echo

# ── 2. Audit log attribution ──────────────────────────────────────
info "${BOLD}Layer 4b — Audit log attribution${RESET}"
null_actor=$(q "SELECT COUNT(*) FROM platform.audit_log WHERE actor_user_id IS NULL;" | tr -d '[:space:]')
empty_type=$(q "SELECT COUNT(*) FROM platform.audit_log WHERE event_type IS NULL OR event_type = '';" | tr -d '[:space:]')
bad_details=$(q "SELECT COUNT(*) FROM platform.audit_log WHERE details IS NOT NULL AND jsonb_typeof(details) NOT IN ('object', 'array');" | tr -d '[:space:]')
total=$(q "SELECT COUNT(*) FROM platform.audit_log;" | tr -d '[:space:]')
if [ "$null_actor" != "0" ]; then fail "${null_actor} rows have NULL actor_user_id"; EXIT_CODE=1; else ok "actor_user_id: 0 nulls across ${total} rows"; fi
if [ "$empty_type" != "0" ]; then fail "${empty_type} rows have empty event_type"; EXIT_CODE=1; else ok "event_type: 0 empties"; fi
if [ "$bad_details" != "0" ]; then fail "${bad_details} rows have details that are not object or array"; EXIT_CODE=1; else ok "details JSONB: every non-null row is object or array"; fi
echo

# ── 3. Session invariants ──────────────────────────────────────────
info "${BOLD}Layer 4c — Session invariants${RESET}"
dupe_open=$(q "SELECT COUNT(*) FROM (SELECT user_id, service_name, COUNT(*) AS n FROM platform.service_sessions WHERE closed_at IS NULL GROUP BY user_id, service_name HAVING COUNT(*) > 1) sub;" | tr -d '[:space:]')
if [ "$dupe_open" != "0" ]; then
  fail "${dupe_open} (user, service) pairs have MORE THAN ONE open session — session-dedup invariant broken"
  EXIT_CODE=1
else
  ok "no (user, service) has >1 open session — dedup invariant intact"
fi
echo

# ── 4. Permission consistency ─────────────────────────────────────
info "${BOLD}Layer 4d — Permission consistency${RESET}"
wildcard_and_explicit=$(q "SELECT COUNT(DISTINCT role_id) FROM platform.role_permissions rp WHERE service_name = '*' AND EXISTS (SELECT 1 FROM platform.role_permissions rp2 WHERE rp2.role_id = rp.role_id AND rp2.service_name != '*');" | tr -d '[:space:]')
if [ "$wildcard_and_explicit" != "0" ]; then
  warn "${wildcard_and_explicit} role(s) hold BOTH a wildcard row AND explicit per-service rows"
  warn "  — not necessarily a bug (a partial save from a wildcard state can produce this) but"
  warn "  — investigate: the effective grant becomes ambiguous; the wildcard-to-explicit rewrite"
  warn "  — described in the report Section 4.2.4 was intended to prevent this state."
else
  ok "no role mixes wildcard and per-service rows"
fi
echo

# ── 5. Pipeline runs' JSONB shape ─────────────────────────────────
info "${BOLD}Layer 4e — Pipeline runs step_results JSONB shape${RESET}"
# The exact regression from §4.4 — step_results should decode as an
# array of objects. Anything else (string / null / scalar) means the
# asyncpg JSONB codec is bypassed and downstream consumers see the
# encoded string.
bad_shape=$(q "SELECT COUNT(*) FROM platform.pipeline_runs WHERE step_results IS NOT NULL AND jsonb_typeof(step_results) != 'array';" | tr -d '[:space:]')
runs_total=$(q "SELECT COUNT(*) FROM platform.pipeline_runs;" | tr -d '[:space:]')
if [ "$bad_shape" != "0" ]; then
  fail "${bad_shape}/${runs_total} pipeline_runs have step_results that is NOT a JSONB array"
  fail "  — the asyncpg JSONB codec is being bypassed; the runs page will show every step as 'queued'"
  fail "  — see report Section 4.4 for the historical incident and the codec-registration fix in db.py"
  EXIT_CODE=1
elif [ "$runs_total" = "0" ]; then
  warn "no pipeline runs exist yet — trigger one via testing/runbooks/pipeline-manual-run.md then re-run this check"
else
  ok "${runs_total}/${runs_total} pipeline_runs decode as a JSONB array"
fi
echo

# ── 6. Orphan detection ───────────────────────────────────────────
info "${BOLD}Layer 4f — Orphan detection${RESET}"
orphan_sessions=$(q "SELECT COUNT(*) FROM platform.service_sessions ss WHERE NOT EXISTS (SELECT 1 FROM platform.users u WHERE u.id = ss.user_id);" | tr -d '[:space:]')
orphan_perms=$(q "SELECT COUNT(*) FROM platform.role_permissions rp WHERE NOT EXISTS (SELECT 1 FROM platform.roles r WHERE r.id = rp.role_id);" | tr -d '[:space:]')
orphan_steps=$(q "SELECT COUNT(*) FROM platform.pipeline_steps ps WHERE NOT EXISTS (SELECT 1 FROM platform.pipelines p WHERE p.id = ps.pipeline_id);" | tr -d '[:space:]')
[ "$orphan_sessions" = "0" ] && ok "service_sessions: 0 orphans (all reference a valid user_id)" || { fail "${orphan_sessions} service_sessions with no matching user"; EXIT_CODE=1; }
[ "$orphan_perms" = "0" ]    && ok "role_permissions: 0 orphans (all reference a valid role_id)" || { fail "${orphan_perms} role_permissions with no matching role"; EXIT_CODE=1; }
[ "$orphan_steps" = "0" ]    && ok "pipeline_steps: 0 orphans (all reference a valid pipeline_id)" || { fail "${orphan_steps} pipeline_steps with no matching pipeline"; EXIT_CODE=1; }
echo

# ── Summary ───────────────────────────────────────────────────────
if [ "$EXIT_CODE" -eq 0 ]; then
  printf '%s%sAll database invariants intact.%s\n' "${BOLD}" "${GREEN}" "${RESET}"
else
  printf '%s%sDB audit found problems above. Exit code: %d%s\n' \
    "${BOLD}" "${RED}" "${EXIT_CODE}" "${RESET}"
fi
exit "$EXIT_CODE"
