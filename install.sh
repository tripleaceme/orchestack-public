#!/usr/bin/env bash
# OrcheStack — one-line installer.
#
# Usage (recommended):
#   curl -sSL https://orchestack.africa/install.sh | bash
#
# Usage (inspect first — recommended for paranoid operators):
#   curl -sSL https://orchestack.africa/install.sh -o install.sh
#   less install.sh           # read it
#   bash install.sh           # then run it
#
# Environment overrides (set before running):
#   ORCHESTACK_VERSION=v0.1.0     pin a specific release (default: latest)
#   ORCHESTACK_DIR=./orchestack   where to install (default: ./orchestack)
#   ORCHESTACK_DB_PASSWORD=...    skip the interactive password prompt
#   ORCHESTACK_NO_START=1         download + write .env only; don't run compose
#
# What this script does (you can verify by reading it):
#   1. Checks that docker + docker compose are installed.
#   2. Downloads the latest release tarball from GitHub releases.
#   3. Extracts it into ./orchestack/ (or $ORCHESTACK_DIR).
#   4. Prompts you for an ORCHESTACK_DB_PASSWORD if you didn't pre-set it.
#   5. Writes .env from .env.example with your password substituted.
#   6. Runs `docker compose up -d`.
#   7. Waits for healthy, prints the URL to visit.
#
# It does NOT: pull individual Docker images manually, modify your shell
# config, install Docker for you, or run anything as root. Everything happens
# inside the install directory.

set -euo pipefail

# ----------------------------------------------------------------------------
# Configuration (overridable via env vars)
# ----------------------------------------------------------------------------
ORCHESTACK_VERSION="${ORCHESTACK_VERSION:-latest}"
ORCHESTACK_DIR="${ORCHESTACK_DIR:-./orchestack}"
ORCHESTACK_REPO="tripleaceme/orchestack-public"

# ----------------------------------------------------------------------------
# Pretty output (ANSI colours, gracefully degrade for non-TTY)
# ----------------------------------------------------------------------------
if [ -t 1 ]; then
  BOLD=$'\033[1m'; DIM=$'\033[2m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; RED=$'\033[31m'; CYAN=$'\033[36m'; RESET=$'\033[0m'
else
  BOLD=""; DIM=""; GREEN=""; YELLOW=""; RED=""; CYAN=""; RESET=""
fi

say()  { printf '%s\n' "${CYAN}▸${RESET} $*"; }
ok()   { printf '%s\n' "${GREEN}✓${RESET} $*"; }
warn() { printf '%s\n' "${YELLOW}!${RESET} $*"; }
die()  { printf '%s\n' "${RED}✗${RESET} $*" >&2; exit 1; }

# ----------------------------------------------------------------------------
# Pre-flight: detect host + tools
# ----------------------------------------------------------------------------
say "OrcheStack installer (${BOLD}${ORCHESTACK_VERSION}${RESET})"
echo

# OS detection — Linux and macOS are supported; Windows users should use WSL2.
case "$(uname -s)" in
  Linux*)   HOST_OS="linux"  ;;
  Darwin*)  HOST_OS="macos"  ;;
  *)        die "Unsupported OS: $(uname -s). OrcheStack runs on Linux or macOS (use WSL2 on Windows)." ;;
esac
ok "Host OS: ${HOST_OS}"

# Required tools — fail loudly with actionable messages.
command -v docker >/dev/null 2>&1 \
  || die "Docker is not installed. Install Docker Desktop (macOS) or Docker Engine (Linux): https://docs.docker.com/get-docker/"
ok "Docker found: $(docker --version | head -1)"

docker compose version >/dev/null 2>&1 \
  || die "Docker Compose v2 is not available. Update Docker Desktop, or install the compose plugin: https://docs.docker.com/compose/install/"
ok "Compose found: $(docker compose version --short)"

docker info >/dev/null 2>&1 \
  || die "Docker daemon is not running. Start Docker Desktop (macOS) or 'sudo systemctl start docker' (Linux)."
ok "Docker daemon is responsive"

# curl is what we used to download THIS script, so it's a safe bet; we still check.
command -v curl >/dev/null 2>&1 || die "curl is required but not installed."
command -v tar  >/dev/null 2>&1 || die "tar is required but not installed."
echo

# ----------------------------------------------------------------------------
# Resolve the download URL
# ----------------------------------------------------------------------------
if [ "${ORCHESTACK_VERSION}" = "latest" ]; then
  # /releases/latest/download/<name> is a stable redirect to the most recent
  # release's asset. No need to hit the GitHub API.
  TARBALL_URL="https://github.com/${ORCHESTACK_REPO}/releases/latest/download/orchestack-runtime.tar.gz"
  CHECKSUM_URL="https://github.com/${ORCHESTACK_REPO}/releases/latest/download/orchestack-runtime.tar.gz.sha256"
else
  # Pinned version: assets are named with the version embedded.
  V="${ORCHESTACK_VERSION#v}"   # strip leading 'v' if the user passed v0.1.0
  TARBALL_URL="https://github.com/${ORCHESTACK_REPO}/releases/download/v${V}/orchestack-runtime-${V}.tar.gz"
  CHECKSUM_URL="${TARBALL_URL}.sha256"
fi

# ----------------------------------------------------------------------------
# Install directory — refuse to clobber an existing install
# ----------------------------------------------------------------------------
if [ -e "${ORCHESTACK_DIR}" ]; then
  if [ -f "${ORCHESTACK_DIR}/docker-compose.yml" ]; then
    die "${ORCHESTACK_DIR} already contains an OrcheStack install. To re-install, remove it first: rm -rf ${ORCHESTACK_DIR}"
  else
    die "${ORCHESTACK_DIR} exists and is not empty. Pick a different ORCHESTACK_DIR or remove it."
  fi
fi

# Use a temp dir for the download so a failed extract doesn't leave debris
# in the install location.
TMPDIR="$(mktemp -d)"
trap 'rm -rf "${TMPDIR}"' EXIT

# ----------------------------------------------------------------------------
# Download + verify
# ----------------------------------------------------------------------------
say "Downloading runtime bundle from GitHub releases..."
curl -fsSL "${TARBALL_URL}"  -o "${TMPDIR}/bundle.tar.gz"  || die "Failed to download bundle from ${TARBALL_URL}"
curl -fsSL "${CHECKSUM_URL}" -o "${TMPDIR}/bundle.sha256"  || warn "No checksum file available (skipping verification)"

if [ -f "${TMPDIR}/bundle.sha256" ]; then
  EXPECTED=$(awk '{print $1}' "${TMPDIR}/bundle.sha256")
  if command -v sha256sum >/dev/null 2>&1; then
    ACTUAL=$(sha256sum "${TMPDIR}/bundle.tar.gz" | awk '{print $1}')
  else
    # macOS ships `shasum -a 256` but not `sha256sum` by default
    ACTUAL=$(shasum -a 256 "${TMPDIR}/bundle.tar.gz" | awk '{print $1}')
  fi
  [ "${EXPECTED}" = "${ACTUAL}" ] || die "Checksum mismatch — refusing to install. Expected ${EXPECTED}, got ${ACTUAL}."
  ok "Checksum verified: ${ACTUAL:0:16}..."
fi
echo

# ----------------------------------------------------------------------------
# Extract — the tarball contains orchestack-runtime-${VERSION}/ as its top dir.
# We extract its CONTENTS into ORCHESTACK_DIR (strip the leading component)
# so the operator gets a clean install path.
# ----------------------------------------------------------------------------
mkdir -p "${ORCHESTACK_DIR}"
tar -xzf "${TMPDIR}/bundle.tar.gz" -C "${ORCHESTACK_DIR}" --strip-components=1
ok "Extracted to ${ORCHESTACK_DIR}/"

if [ -f "${ORCHESTACK_DIR}/VERSION" ]; then
  ACTUAL_VERSION=$(cat "${ORCHESTACK_DIR}/VERSION")
  ok "Installed version: ${ACTUAL_VERSION}"
fi
echo

# ----------------------------------------------------------------------------
# Build .env from .env.example with the operator's password
# ----------------------------------------------------------------------------
cd "${ORCHESTACK_DIR}"

if [ -z "${ORCHESTACK_DB_PASSWORD:-}" ]; then
  # Interactive prompt. Hide the input. Confirm by asking twice.
  #
  # `read </dev/tty` is load-bearing when this script is invoked via
  # `curl -fsSL ... | bash`. In that mode the script's stdin IS the
  # curl pipe, not the terminal; a bare `read` returns an empty string
  # instantly and the empty-password branch below then loops forever.
  # Reading explicitly from /dev/tty bypasses the pipe and takes input
  # from the user's actual terminal. Falls back to plain `read` if no
  # tty is available (CI environments, some SSH pipelines), which is
  # the case where the operator should have set ORCHESTACK_DB_PASSWORD
  # as an env var anyway.
  if [ -r /dev/tty ]; then TTY_IN=/dev/tty; else TTY_IN=/dev/stdin; fi
  printf '%sSet a strong password for OrcheStack'\''s internal database.%s\n' "${BOLD}" "${RESET}"
  printf '%sThis is the bootstrap superuser for OrcheStack metadata only — your%s\n' "${DIM}" "${RESET}"
  printf '%spipeline DB credentials are collected later in the browser wizard.%s\n\n' "${DIM}" "${RESET}"
  while :; do
    read -r -s -p "ORCHESTACK_DB_PASSWORD: " PW1 <"${TTY_IN}"; echo
    read -r -s -p "Confirm:               " PW2 <"${TTY_IN}"; echo
    [ -z "${PW1}" ]      && { warn "Password cannot be empty."; continue; }
    [ "${PW1}" = "${PW2}" ] || { warn "Passwords don't match. Try again."; continue; }
    [ "${#PW1}" -ge 12 ]    || { warn "Password is shorter than 12 characters. Try again (or set ORCHESTACK_DB_PASSWORD env var to bypass this check)."; continue; }
    ORCHESTACK_DB_PASSWORD="${PW1}"
    break
  done
fi

# Substitute the password into .env. We use a sentinel match so we never
# accidentally replace something else, and we use awk (not sed) to avoid
# delimiter issues if the password contains slashes or ampersands.
awk -v pw="${ORCHESTACK_DB_PASSWORD}" '
  /^ORCHESTACK_DB_PASSWORD=/ { print "ORCHESTACK_DB_PASSWORD=" pw; next }
  { print }
' .env.example > .env
chmod 600 .env   # only the operator should be able to read this
ok "Wrote .env (chmod 600)"
echo

# ----------------------------------------------------------------------------
# Bring up the stack
# ----------------------------------------------------------------------------
if [ "${ORCHESTACK_NO_START:-0}" = "1" ]; then
  echo "${DIM}ORCHESTACK_NO_START=1 set — skipping 'docker compose up'.${RESET}"
  echo
  echo "To start manually:"
  echo "  cd ${ORCHESTACK_DIR}"
  echo "  docker compose up -d"
  exit 0
fi

say "Pulling images and starting containers (this may take ~30s on first run)..."
docker compose up -d

# Wait for the auth container to start serving — that's the user-visible
# "ready" signal. Postgres takes ~20s to init, auth depends on it indirectly.
echo
say "Waiting for the platform to come online..."
for i in $(seq 1 60); do
  if docker compose ps --status running --quiet auth 2>/dev/null | grep -q .; then
    if curl -fsS http://localhost/login >/dev/null 2>&1; then
      ok "Auth container is responding."
      break
    fi
  fi
  sleep 1
done

echo
# ────────────────────────────────────────────────────────────────
# Banner shown at the "platform is reachable" moment. Same style
# and same visual role as Airflow's webserver-startup banner.
# Single-quoted heredoc so ` and $ inside the ASCII art aren't
# interpolated by the shell.
# ────────────────────────────────────────────────────────────────
printf '%s' "${BOLD}${CYAN}"
cat <<'BANNER'
    ___           _         ____  _             _
   / _ \ _ __ ___| |__   __/ ___|| |_ __ _  ___| | __
  | | | | '__/ __| '_ \ / _\___ \| __/ _` |/ __| |/ /
  | |_| | | | (__| | | |  __/___) | || (_| | (__|   <
   \___/|_|  \___|_| |_|\___|____/ \__\__,_|\___|_|\_\
BANNER
printf '%s\n' "${RESET}"
printf '%s v%s is up.%s\n\n' "${BOLD}${GREEN}OrcheStack" "${ACTUAL_VERSION}" "${RESET}"

echo "  Open ${CYAN}http://localhost${RESET} to sign up."
echo
echo "  Useful commands (from ${ORCHESTACK_DIR}/):"
echo "    docker compose ps        # service status"
echo "    docker compose logs -f   # tail logs"
echo "    docker compose down      # stop everything (data preserved)"
echo "    docker compose down -v   # stop AND wipe the database volume"
echo
echo "  Docs: ${CYAN}https://orchestack.africa/docs/${RESET}"
