#!/usr/bin/env bash
#
# OrcheStack — operator-facing upgrade script.
# Ships inside every runtime bundle at /upgrade.sh.
#
# OrcheStack's upgrade story has two parts:
#   1. Docker images (auth, dashboard, orchestrator, airflow, ge) —
#      pulled from Docker Hub
#   2. Runtime config (docker-compose.yml, services/*.yml, traefik/,
#      postgres-init/) — lives in the GitHub Release tarball, NOT in
#      any image
#
# This script does both. It backs up .env, fetches the latest tarball,
# replaces the runtime config in place, pulls new images, restarts.
#
# Safe to re-run: each step is idempotent; .env is backed up to a
# timestamped file before any overwrite.
#
# Usage:
#   ./upgrade.sh            # upgrade to the latest published release
#   ./upgrade.sh v0.1.2     # upgrade to a specific release (NOT YET SUPPORTED)

set -euo pipefail

# ---- Pre-flight --------------------------------------------------------
if [ ! -f docker-compose.yml ] || [ ! -f .env ]; then
  echo "ERROR: this script must be run from inside your OrcheStack runtime"
  echo "       install directory (the one that has docker-compose.yml and"
  echo "       .env). cd into orchestack-runtime-X.Y.Z/ first."
  exit 1
fi

if ! command -v docker >/dev/null 2>&1; then
  echo "ERROR: docker not found on PATH. Install Docker Engine first."
  exit 1
fi

if ! docker info >/dev/null 2>&1; then
  echo "ERROR: docker daemon not reachable. Start Docker Desktop / dockerd."
  exit 1
fi

# ---- Variables ---------------------------------------------------------
RELEASE_URL="https://github.com/tripleaceme/orchestack-public/releases/latest/download/orchestack-runtime.tar.gz"
TS="$(date +%Y%m%d-%H%M%S)"
ENV_BACKUP=".env.bak.${TS}"
STAGING="$(mktemp -d -t orchestack-upgrade-XXXXXX)"
trap 'rm -rf "${STAGING}"' EXIT

CURRENT_VERSION="$(cat VERSION 2>/dev/null || echo 'unknown')"

# ---- Banner ------------------------------------------------------------
cat <<EOF
═══════════════════════════════════════════════════════════════════
  OrcheStack upgrade
  Current version: ${CURRENT_VERSION}
  Target:          latest published release on GitHub
  Working dir:     $(pwd)
═══════════════════════════════════════════════════════════════════
EOF

# ---- 1. Backup .env ----------------------------------------------------
echo
echo "→ [1/5] backing up .env to ${ENV_BACKUP}"
cp .env "${ENV_BACKUP}"
echo "  backup saved (your passwords + REPO URLs preserved)"

# ---- 2. Download the latest tarball -----------------------------------
echo
echo "→ [2/5] downloading the latest runtime bundle"
echo "  source: ${RELEASE_URL}"
if ! curl -fsSL -o "${STAGING}/runtime.tar.gz" "${RELEASE_URL}"; then
  echo "  ERROR: download failed. Check your network + that the GitHub"
  echo "         Release page is reachable. Your install is unchanged."
  exit 1
fi
SIZE=$(stat -f%z "${STAGING}/runtime.tar.gz" 2>/dev/null || stat -c%s "${STAGING}/runtime.tar.gz" 2>/dev/null)
echo "  downloaded $((SIZE / 1024)) KB"

# ---- 3. Extract to staging + replace runtime files --------------------
echo
echo "→ [3/5] extracting + replacing runtime config"
tar xzf "${STAGING}/runtime.tar.gz" -C "${STAGING}"
NEW_BUNDLE_DIR=$(find "${STAGING}" -maxdepth 1 -type d -name "orchestack-runtime-*" | head -1)
if [ -z "${NEW_BUNDLE_DIR}" ]; then
  echo "  ERROR: extracted tarball doesn't contain the expected"
  echo "         orchestack-runtime-X.Y.Z/ directory. Bundle layout may"
  echo "         have changed. Your install is unchanged."
  exit 1
fi
NEW_VERSION=$(cat "${NEW_BUNDLE_DIR}/VERSION" 2>/dev/null || echo 'unknown')
echo "  new version: ${NEW_VERSION}"

if [ "${NEW_VERSION}" = "${CURRENT_VERSION}" ]; then
  echo "  NOTE: target version matches current. Continuing — image pulls"
  echo "        may still bring in a re-cut of the same semver tag."
fi

# Replace runtime files in place. .env, VERSION (will be replaced next),
# and any operator-added files are left alone.
cp    "${NEW_BUNDLE_DIR}/docker-compose.yml"      ./docker-compose.yml
cp    "${NEW_BUNDLE_DIR}/INSTALL.md"              ./INSTALL.md
cp    "${NEW_BUNDLE_DIR}/upgrade.sh"              ./upgrade.sh
chmod +x ./upgrade.sh
cp    "${NEW_BUNDLE_DIR}/VERSION"                 ./VERSION
cp -R "${NEW_BUNDLE_DIR}/services/."              ./services/
cp -R "${NEW_BUNDLE_DIR}/traefik/."               ./traefik/
cp -R "${NEW_BUNDLE_DIR}/postgres-init/."         ./postgres-init/
# .env.example is updated too so the operator sees any new keys/comments
# in `.env.example` if they later need to add a missing one to their .env.
cp    "${NEW_BUNDLE_DIR}/.env.example"            ./.env.example
echo "  runtime config replaced (your .env preserved)"

# ---- 4. Pull new images -----------------------------------------------
echo
echo "→ [4/5] pulling new Docker images (this can take several minutes"
echo "        on first-pull of heavy images like orchestack-airflow)"
docker compose pull

# ---- 5. Restart with new config + images ------------------------------
echo
echo "→ [5/5] restarting stack with the new config + images"
docker compose up -d

# ---- Success ----------------------------------------------------------
cat <<EOF

═══════════════════════════════════════════════════════════════════
  ✓ Upgrade complete

  Old version: ${CURRENT_VERSION}
  New version: ${NEW_VERSION}

  Your .env was backed up to: ${ENV_BACKUP}
  Per-service state (passwords, dashboards, dbt project, etc.)
  preserved in their docker volumes.

  Wait ~30 seconds for the control plane to fully restart, then
  visit your dashboard at http://localhost/app/
═══════════════════════════════════════════════════════════════════
EOF
