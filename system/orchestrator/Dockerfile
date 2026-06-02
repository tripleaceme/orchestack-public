# OrcheStack orchestrator — control-plane service.
#
# Build context note
# ------------------
# This Dockerfile is built with this folder (system/orchestrator/) as the
# context — unlike the auth image which uses the repo root for the shared CSS.
# The orchestrator has no external dependencies outside this folder, so the
# narrower context produces a smaller build and a faster CI run.
#
# To build locally (use `make image-orchestrator` from the repo root):
#   docker build -t tripleaceme/orchestack-orchestrator:dev system/orchestrator
#
# The docker-compose `orchestrator` service `image:` line pulls this from
# Docker Hub once it's been published by the CI workflow.

# python:3.12-slim is ~50 MB, much smaller than the alpine variants for Python
# (alpine + Python has historically had wheel compatibility issues — Debian
# slim avoids that without much size penalty).
FROM python:3.12-slim

# Set up the application directory + runtime user. We DON'T run as root —
# the orchestrator has no need for root privileges and reducing the
# blast radius costs us nothing.
WORKDIR /app
RUN groupadd --system orchestrator \
 && useradd  --system --gid orchestrator --home-dir /app orchestrator

# Install Python dependencies. Copy requirements.txt first so dependency
# installation is cached separately from the application code — `pip install`
# runs only when the requirements file changes, not on every code edit.
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Copy the application code AFTER dependency installation so changes to
# main.py don't invalidate the dependency cache.
COPY app/ /app/app/

# Run as the unprivileged user from here on.
USER orchestrator

# Container health check — probes the FastAPI /api/health endpoint.
# Uses python's urllib (already in the standard library) so we don't add
# curl/wget as runtime dependencies just to call our own HTTP endpoint.
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request, sys; \
    r = urllib.request.urlopen('http://localhost:8000/api/health', timeout=2); \
    sys.exit(0 if r.status == 200 else 1)" || exit 1

EXPOSE 8000

# Launch FastAPI under uvicorn. --host 0.0.0.0 binds inside the container
# only (Docker handles the network namespace); --workers=1 because the
# reconciler loop must own its database connection pool, and running
# multiple workers would duplicate the reconciler. Scale horizontally by
# running a second container, not by adding workers within one.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
