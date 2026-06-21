# OrcheStack in-package auth + setup container.
#
# Build context note
# ------------------
# This Dockerfile expects the build context to be the OrcheStack REPO ROOT,
# not the system/auth/ folder. That's because the container ships the shared
# CSS from assets/css/ (the single canonical source used by both the public
# marketing site AND the in-container pages), which lives outside this folder.
# All COPY paths below are relative to OrcheStack/.
#
# To build locally:
#   cd OrcheStack/
#   docker build -t tripleaceme/orchestack-auth:dev -f system/auth/Dockerfile .
#
# The docker-compose service uses build.context: ../.. (from system/docker/)
# which resolves to the same OrcheStack root. The CI workflow uses context: .
# at the repo root for the same reason.
#
# What this container serves (runs on every customer's host as part of
# `docker compose up`):
#
#   1. /signup    — first-admin bootstrap
#   2. /login     — admin auth
#   3. /setup/*   — 4-step onboarding wizard (welcome → select → configure → deploying)
#   4. /assets/*  — shared CSS (sourced from OrcheStack/assets/ at build time)
#
# What it does NOT serve: marketing landing page, services catalog, docs,
# contact form — those are only on the public Cloudflare Pages deployment at
# https://orchestack.africa. The architectural reason: docs change frequently
# and we don't want customer installs serving stale documentation. Keeping
# them off the image also keeps the image small.
#
# Brand protection: page content is BAKED into the image at build time (no
# volume mounts). Operators cannot silently re-brand the onboarding wizard
# without forking and rebuilding.

FROM nginx:stable-alpine

# OCI image labels — published-image metadata. Static ones live here;
# build-time labels (revision, version, created) are added by
# docker/metadata-action in the release workflow so each published tag
# carries the commit SHA it was built from.
LABEL org.opencontainers.image.title="orchestack-auth" \
      org.opencontainers.image.description="OrcheStack signup, login, and setup wizard (nginx + static)" \
      org.opencontainers.image.vendor="OrcheStack" \
      org.opencontainers.image.source="https://github.com/tripleaceme/orchestack-public" \
      org.opencontainers.image.documentation="https://orchestack.africa" \
      org.opencontainers.image.licenses="Apache-2.0"

# Remove the default nginx site config so ours takes over.
RUN rm /etc/nginx/conf.d/default.conf

# Site configuration (scoped to /signup, /login, /setup/*, /assets/*).
COPY system/auth/nginx.conf /etc/nginx/conf.d/OrcheStack.conf

# In-container pages: signup.html, login.html, setup/*.html.
# The public/ folder contains an `assets/css` SYMLINK that points up to the
# repo-root assets/css/ — this exists only for local development (so that
# `python3 -m http.server --directory system/auth/public` can resolve the
# stylesheet without the operator running a Docker build). It is NOT the
# canonical source.
COPY system/auth/public/ /usr/share/nginx/html/

# Wipe whatever the previous COPY left at /assets/ — specifically the dev-only
# symlink at assets/css/ — so the next COPY writes plain regular files into a
# clean directory. Without this, writing through the broken symlink (its
# target path doesn't exist inside the image) would fail.
RUN rm -rf /usr/share/nginx/html/assets

# Shared CSS from the repo root. This is the SINGLE source of truth for
# styling, used by both the marketing site (orchestack.africa) and these
# in-container pages.
COPY assets/css/ /usr/share/nginx/html/assets/css/

# Container health check — nginx must respond on :80.
# Probes a dedicated /health endpoint instead of a real page so the check
# (a) doesn't depend on any HTML file being present, (b) doesn't generate
# access-log noise, (c) returns instantly with no template work.
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD wget --quiet --spider http://localhost/health || exit 1

EXPOSE 80

CMD ["nginx", "-g", "daemon off;"]
