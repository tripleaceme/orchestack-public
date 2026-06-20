# OrcheStack orchestrator — control-plane service.
#
# Build context note
# ------------------
# This Dockerfile is built with this folder (system/orchestrator/) as the
# context. The orchestrator has no external dependencies outside this folder,
# so the narrower context produces a smaller build and a faster CI run.
#
# To build locally (from the repo root):
#   make image-orchestrator
# or:
#   docker build -t tripleaceme/orchestack-orchestrator:dev system/orchestrator

# python:3.12-slim is ~50 MB, much smaller than the alpine variants for Python
# (alpine + Python has historically had wheel compatibility issues — Debian
# slim avoids that without much size penalty).
FROM python:3.12-slim

# ----------------------------------------------------------------------------
# Install the Docker CLI + compose plugin.
#
# The orchestrator shells out to `docker compose -f services/<x>.yml up -d`
# from app/docker_ops.py. We deliberately use the upstream CLI rather than
# the Python Docker SDK because:
#   1. The CLI does proper API version negotiation. Modern Docker daemons
#      enforce MinAPIVersion=1.40; libraries that hardcode old defaults
#      (which Traefik v3.2 did) silently fail. The CLI doesn't have that
#      problem.
#   2. Operators can reproduce any orchestrator action by running the exact
#      command from their own shell. Debuggability beats abstraction.
#
# Why install from Docker's apt repo and not Debian's: Debian ships an older
# docker package that often lacks the compose plugin we need.
# ----------------------------------------------------------------------------
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates curl gnupg \
 && install -m 0755 -d /etc/apt/keyrings \
 && curl -fsSL https://download.docker.com/linux/debian/gpg \
      | gpg --dearmor -o /etc/apt/keyrings/docker.gpg \
 && chmod a+r /etc/apt/keyrings/docker.gpg \
 && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
         https://download.docker.com/linux/debian $(. /etc/os-release && echo $VERSION_CODENAME) stable" \
      > /etc/apt/sources.list.d/docker.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends \
      docker-ce-cli docker-compose-plugin \
 && rm -rf /var/lib/apt/lists/*

# ----------------------------------------------------------------------------
# Application setup
# ----------------------------------------------------------------------------
WORKDIR /app

# NB: we do NOT create an unprivileged runtime user the way the auth image
# does. The orchestrator container will have /var/run/docker.sock mounted
# from the host so it can manage other containers — and that socket is
# owned by root (or the docker group, depending on the host). Running as
# an unprivileged user would require host-side coordination of the GID,
# which varies wildly across Docker Desktop / Linux / GitHub Actions. We
# accept root-inside-container here because (a) the orchestrator's
# function literally IS container management, and (b) the container has
# nothing to gain from privilege escalation it doesn't already have via
# the socket.

# Install Python dependencies. Copy requirements first so dependency install
# is cached separately from application code edits.
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Copy the application code.
COPY app/ /app/app/

# Container healthcheck — probes the FastAPI /api/health endpoint.
# Uses python stdlib urllib (no curl/wget needed). The auth image hit a
# subtle bug here where alpine's busybox wget resolved localhost to ::1
# and nginx was IPv4-only. python:3.12-slim doesn't have busybox; urllib
# resolves IPv4 first by default. One less footgun.
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request, sys; \
    r = urllib.request.urlopen('http://localhost:8000/api/health', timeout=2); \
    sys.exit(0 if r.status == 200 else 1)" || exit 1

EXPOSE 8000

# --workers=1 because the reconciler loop must own its database connection
# pool. Multiple workers would duplicate the reconciler — services would
# get stopped twice over, audit log would double-log every action. Scale
# horizontally by running a second orchestrator container, not by adding
# workers inside one.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
