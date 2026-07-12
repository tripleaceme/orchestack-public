#!/usr/bin/env bash
# Assemble the public site (orchestack.africa) from the repo's
# static-HTML sources into a single _site/ directory Cloudflare
# Pages can serve directly.
#
# Layout produced:
#   _site/index.html                  ← front-facing landing
#   _site/services.html               ← front-facing services page
#   _site/contact.html                ← front-facing contact page
#   _site/install.sh                  ← operator install script (curl-safe)
#   _site/docs/*.html                 ← 28 operator docs pages
#   _site/docs/services/*.html        ← per-service pages
#   _site/docs/guides/*.html          ← operator guides
#   _site/assets/css/*                ← shared stylesheets
#   _site/assets/images/*             ← shared images (if any)
#
# Cloudflare Pages settings:
#   Build command:      bash scripts/build-site.sh
#   Build output dir:   _site
#   Root directory:     /  (repo root — leave blank)
#
# Rebuild is idempotent — safe to re-run.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

OUT=_site
rm -rf "$OUT"
mkdir -p "$OUT/docs" "$OUT/assets"

# 1. Marketing pages at site root
cp front-facing/*.html "$OUT/"

# 2. Operator install script at /install.sh
cp install.sh "$OUT/install.sh"
chmod +x "$OUT/install.sh"

# 3. Operator docs at /docs/
cp -R docs/. "$OUT/docs/"

# 4. Shared assets at /assets/
cp -R assets/. "$OUT/assets/"

# 5. Strip macOS metadata files that sneak in on the developer laptop
find "$OUT" -name '.DS_Store' -delete

echo "  _site/ built"
find "$OUT" -type f | wc -l | awk '{print "  files: " $1}'
du -sh "$OUT" | awk '{print "  size:  " $1}'
