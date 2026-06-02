#!/usr/bin/env bash
# OrcheStack — local bundle builder.
#
# Mirrors the GitHub Actions release workflow (.github/workflows/release.yml)
# so you can produce the same orchestack-runtime tarball without pushing a
# tag or making the repo public. Useful for:
#
#   - Testing the Option B install path on a separate machine while the
#     source repo is still private (final-year project, pre-release, etc).
#   - Smoke-testing changes to docker-compose.yml or postgres-init/ before
#     tagging a release.
#   - Producing a customised bundle for an internal customer who shouldn't
#     see the upstream source.
#
# Usage:
#   ./scripts/build-bundle.sh                  # builds dev-${SHA}.tar.gz
#   ./scripts/build-bundle.sh 0.1.0-test       # builds custom version label
#   ./scripts/build-bundle.sh --out /tmp       # write tarball somewhere else
#
# Output:
#   <out>/orchestack-runtime-${VERSION}.tar.gz       (versioned, ~13 KB)
#   <out>/orchestack-runtime-${VERSION}.tar.gz.sha256
#   <out>/orchestack-runtime.tar.gz                  (unversioned alias)
#   <out>/orchestack-runtime.tar.gz.sha256
#
# Transfer to another machine:
#   scp orchestack-runtime.tar.gz pi@test-host:~/
#   # or USB stick, AirDrop, email, etc. — it's 13 KB, all flat text.
#
# On the receiving machine:
#   tar xzf orchestack-runtime.tar.gz
#   cd orchestack-runtime-*
#   cp .env.example .env
#   $EDITOR .env              # set ORCHESTACK_DB_PASSWORD
#   docker compose up -d      # pulls the public Docker Hub images
#
# Docker Hub images are public, so `docker compose up` works from any host
# with internet access — no GitHub credentials needed.

set -euo pipefail

# ----------------------------------------------------------------------------
# Resolve paths — script lives at OrcheStack/scripts/, so the repo root is ..
# ----------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# ----------------------------------------------------------------------------
# Parse args — first positional = version label, --out flag = output dir
# ----------------------------------------------------------------------------
VERSION=""
OUT_DIR="${REPO_ROOT}"
while [ $# -gt 0 ]; do
  case "$1" in
    --out)  OUT_DIR="$2"; shift 2 ;;
    -h|--help)
      sed -n '2,/^set -euo/p' "$0" | sed 's/^# \?//' | head -40
      exit 0
      ;;
    *)      VERSION="$1"; shift ;;
  esac
done

# Default version: dev-${short-sha} so each commit gets a unique tarball name
# and you don't accidentally overwrite an earlier bundle.
if [ -z "${VERSION}" ]; then
  if command -v git >/dev/null 2>&1 && git -C "${REPO_ROOT}" rev-parse --short HEAD >/dev/null 2>&1; then
    SHA="$(git -C "${REPO_ROOT}" rev-parse --short HEAD)"
    VERSION="dev-${SHA}"
  else
    VERSION="dev-local"
  fi
fi

BUNDLE_NAME="orchestack-runtime-${VERSION}"
mkdir -p "${OUT_DIR}"

# ----------------------------------------------------------------------------
# Pretty output
# ----------------------------------------------------------------------------
if [ -t 1 ]; then GREEN=$'\033[32m'; CYAN=$'\033[36m'; DIM=$'\033[2m'; RESET=$'\033[0m'
else              GREEN=""; CYAN=""; DIM=""; RESET=""; fi

echo "${CYAN}▸${RESET} Building OrcheStack runtime bundle"
echo "    version: ${VERSION}"
echo "    output:  ${OUT_DIR}/"
echo

# ----------------------------------------------------------------------------
# Assemble bundle in a temp dir, then tar it
# ----------------------------------------------------------------------------
STAGING="$(mktemp -d)"
trap 'rm -rf "${STAGING}"' EXIT

BUNDLE="${STAGING}/${BUNDLE_NAME}"
mkdir -p "${BUNDLE}/traefik/dynamic" "${BUNDLE}/postgres-init" "${BUNDLE}/services"

cp "${REPO_ROOT}/system/docker/docker-compose.yml"        "${BUNDLE}/docker-compose.yml"
cp "${REPO_ROOT}/system/docker/.env.example"              "${BUNDLE}/.env.example"
cp "${REPO_ROOT}/system/docker/traefik/traefik.yml"       "${BUNDLE}/traefik/traefik.yml"
touch                                                     "${BUNDLE}/traefik/dynamic/.gitkeep"
cp "${REPO_ROOT}"/system/docker/postgres-init/*.sql       "${BUNDLE}/postgres-init/"
# Per-service compose snippets — what the orchestrator brings up on demand
# (M2.3+). One YAML per cold/hot-tier service; mounted read-only by the
# orchestrator at /services inside its container.
cp "${REPO_ROOT}"/system/docker/services/*.yml            "${BUNDLE}/services/"

echo "${VERSION}" > "${BUNDLE}/VERSION"

cat > "${BUNDLE}/INSTALL.md" <<EOF
# OrcheStack ${VERSION} — local install bundle

This bundle was built locally (not from a GitHub Release). The runtime
files are identical to what a tagged release produces, but the bundle
was assembled on the developer's machine via \`scripts/build-bundle.sh\`.

## Install

\`\`\`sh
cp .env.example .env
\$EDITOR .env                  # set ORCHESTACK_DB_PASSWORD
docker compose up -d
\`\`\`

Visit http://localhost and sign up.

## Updating

Get a fresh tarball from the developer (or once the repo goes public, from
https://github.com/tripleaceme/orchestack/releases/latest) and replace the
files in this directory. Keep your \`.env\` — never overwrite it.
EOF

# ----------------------------------------------------------------------------
# Build the tarball — portable flags only (works on both GNU and BSD tar)
# ----------------------------------------------------------------------------
tar -czf "${OUT_DIR}/${BUNDLE_NAME}.tar.gz" -C "${STAGING}" "${BUNDLE_NAME}"

# Checksum (uses sha256sum on Linux, shasum -a 256 on macOS)
cd "${OUT_DIR}"
if command -v sha256sum >/dev/null 2>&1; then
  sha256sum "${BUNDLE_NAME}.tar.gz" > "${BUNDLE_NAME}.tar.gz.sha256"
else
  shasum -a 256 "${BUNDLE_NAME}.tar.gz" > "${BUNDLE_NAME}.tar.gz.sha256"
fi

# Unversioned aliases — for the eventual "latest" URL pattern. Locally,
# this makes the file easy to scp by a stable name across iterations.
cp "${BUNDLE_NAME}.tar.gz"        "orchestack-runtime.tar.gz"
cp "${BUNDLE_NAME}.tar.gz.sha256" "orchestack-runtime.tar.gz.sha256"
# Rewrite the checksum line so `sha256sum -c` looks for the unversioned file
if command -v gsed >/dev/null 2>&1; then
  gsed -i "s| ${BUNDLE_NAME}.tar.gz| orchestack-runtime.tar.gz|" "orchestack-runtime.tar.gz.sha256"
else
  sed -i.bak "s| ${BUNDLE_NAME}.tar.gz| orchestack-runtime.tar.gz|" "orchestack-runtime.tar.gz.sha256"
  rm -f "orchestack-runtime.tar.gz.sha256.bak"
fi

echo "${GREEN}✓${RESET} Bundle written:"
ls -lh "${BUNDLE_NAME}.tar.gz" "${BUNDLE_NAME}.tar.gz.sha256" \
       "orchestack-runtime.tar.gz" "orchestack-runtime.tar.gz.sha256" | awk '{print "    " $5 "  " $9}'

echo
echo "${DIM}Next steps:${RESET}"
echo "  1. Transfer ${CYAN}orchestack-runtime.tar.gz${RESET} to your test machine (any method)."
echo "  2. On the test machine:"
echo "       tar xzf orchestack-runtime.tar.gz"
echo "       cd orchestack-runtime-*"
echo "       cp .env.example .env && \$EDITOR .env       # set ORCHESTACK_DB_PASSWORD"
echo "       docker compose up -d"
echo "  3. Visit http://localhost on the test machine to sign up."
