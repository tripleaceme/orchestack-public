# OrcheStack dashboard — administrator UI.
#
# Build context: this folder (system/dashboard/). The dashboard has no
# external dependencies outside this folder, so the narrower context
# produces a smaller build and a faster CI run — same pattern as the
# orchestrator image.
#
# To build locally (from the repo root):
#   make image-dashboard
# or:
#   docker build -t tripleaceme/orchestack-dashboard:dev system/dashboard

# python:3.12-slim — same base as the orchestrator. Reusing the base layer
# means the Docker layer cache is shared across both image builds on the
# CI runner, ~30s faster average build time.
FROM python:3.12-slim

WORKDIR /app

# Create an unprivileged runtime user. Unlike the orchestrator (which needs
# root for Docker socket access), the dashboard has zero need for
# privileges — it only makes HTTP calls and serves HTML.
RUN groupadd --system dashboard \
 && useradd  --system --gid dashboard --home-dir /app dashboard

# Install Python dependencies. Copy requirements first so dep install is
# cached separately from application code edits.
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Application code.
COPY app/ /app/app/

# CI passes --build-arg DASHBOARD_BUILD_SHA=$GITHUB_SHA so the dashboard
# footer can display "dashboard <8-char-sha>" instead of "dev." Default
# stays "dev" for local builds so the footer is still readable in dev.
ARG DASHBOARD_BUILD_SHA=dev
ENV DASHBOARD_BUILD_SHA=$DASHBOARD_BUILD_SHA

USER dashboard

# Container healthcheck via stdlib urllib (no curl/wget dependency).
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request, sys; \
    r = urllib.request.urlopen('http://localhost:8000/healthz', timeout=2); \
    sys.exit(0 if r.status == 200 else 1)" || exit 1

EXPOSE 8000

# --workers=1 is fine for the dashboard's expected load (1-10 concurrent
# operators per OrcheStack install). FastAPI's async event loop handles
# the many small orchestrator-API calls efficiently on a single worker.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
