# OrcheStack Great Expectations image.
#
# Pre-installs everything the runtime entrypoint needed to do at first
# start: editors, git, ttyd, and the full GE Python toolchain. Cuts the
# fresh-start time from ~30-60 minutes (apt+pip on slow networks) to
# ~10 seconds (just container creation).
#
# Build locally:
#   docker build -t tripleaceme/orchestack-ge:0.18.21-1 \
#       -f system/docker/ge-image/Dockerfile system/docker/ge-image/
#
# Push:
#   docker push tripleaceme/orchestack-ge:0.18.21-1
#
# Bump the tag suffix (-1, -2, ...) when you change the Dockerfile so
# operators on existing installs can `docker compose pull` to upgrade
# without changing the base GE version.

FROM python:3.12-slim

# ── 1. System packages: editors + git + curl + libpq for psycopg2 ────
# DEBIAN_FRONTEND=noninteractive prevents prompts from blocking the
# install (tzdata etc.). --no-install-recommends keeps image size down.
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        nano \
        vim-tiny \
        less \
        git \
        curl \
        ca-certificates \
        libpq5 \
    && rm -rf /var/lib/apt/lists/*

# ── 2. ttyd web terminal — static binary from GitHub releases ────────
RUN curl -fL -o /usr/local/bin/ttyd \
        https://github.com/tsl0922/ttyd/releases/download/1.7.7/ttyd.x86_64 && \
    chmod +x /usr/local/bin/ttyd

# ── 3. Great Expectations + postgres driver ──────────────────────────
# Installed into a venv at /opt/ge-venv so the runtime entrypoint can
# activate it via PATH=/opt/ge-venv/bin:$PATH (same shape as the
# pip-in-entrypoint version, just baked at build time).
RUN python -m venv /opt/ge-venv && \
    /opt/ge-venv/bin/pip install --no-cache-dir --upgrade pip && \
    /opt/ge-venv/bin/pip install --no-cache-dir \
        'great_expectations==0.18.21' \
        'psycopg2-binary>=2.9' \
        'sqlalchemy>=1.4,<2'

# ── 4. Verify the toolchain works ────────────────────────────────────
# Build fails if any of these can't run — better to find out at build
# time than at container start.
RUN /opt/ge-venv/bin/great_expectations --version && \
    which nano && which git && which curl && which ttyd

# Runtime contract: PATH includes /opt/ge-venv/bin so `great_expectations`
# is on the default PATH. The compose entrypoint can omit the install
# steps and jump straight to bootstrapping the data context.
ENV PATH="/opt/ge-venv/bin:${PATH}"
ENV VIRTUAL_ENV=/opt/ge-venv

# Sensible defaults; overridable from compose.
WORKDIR /great_expectations
