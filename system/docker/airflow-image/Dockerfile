# OrcheStack Airflow image.
#
# Apache Airflow 2.10 with dbt-core, dbt-postgres, and astronomer-cosmos
# preinstalled so the operator's first DAG can drive dbt with per-model
# task granularity without any runtime pip-install.
#
# Why Airflow 2 (not 3): OrcheStack's target users are first-time Airflow
# operators. Every Airflow tutorial on Stack Overflow, Astronomer Academy,
# Marc Lamberti's videos, etc. shows the Airflow 2 UI — the 3.x React
# rewrite makes those resources misleading. Airflow 2.10 is actively
# patched and Cosmos 1.14 supports both 2.10+ and 3.x, so the operator
# gets a familiar UI without losing dbt-orchestration capability.
# We'll bump to 3.x when ecosystem docs catch up.
#
# Build context: this folder (system/docker/airflow-image/).
#
# Locally:
#   make image-airflow            # produces tripleaceme/orchestack-airflow:dev
#
# Tag bumping policy: bump alongside the rest of the OrcheStack release
# tags (the release.yml docker-publish matrix sets both :latest and the
# semver tag on every image at once).

FROM apache/airflow:2.10.5-python3.12

LABEL org.opencontainers.image.title="orchestack-airflow" \
      org.opencontainers.image.description="Apache Airflow 2.10 with dbt-core, dbt-postgres, and astronomer-cosmos preinstalled for OrcheStack" \
      org.opencontainers.image.vendor="OrcheStack" \
      org.opencontainers.image.source="https://github.com/tripleaceme/orchestack-public" \
      org.opencontainers.image.documentation="https://orchestack.africa" \
      org.opencontainers.image.licenses="Apache-2.0"

# ────────────────────────────────────────────────────────────────────────
# dbt + Cosmos pre-installation
# ────────────────────────────────────────────────────────────────────────
# Why bake in (vs runtime _PIP_ADDITIONAL_REQUIREMENTS):
#   - Cold start: 5-10s vs 60-90s (no per-start pip resolution)
#   - Reliability: build fails LOUDLY if a version pin breaks; runtime
#     install would fail SILENTLY mid-task
#   - Reproducibility: every operator pulling :0.1.0 gets the same
#     exact dbt + cosmos versions
#
# Version pins:
#   - dbt-core 1.8.x          : dbt's protobuf dependency has crept up
#                               minor-by-minor:
#                                 1.10.* → protobuf>=6,<7   (incompatible)
#                                 1.9.9+ → protobuf>=6,<7   (incompatible)
#                                 1.9.0–1.9.8 → protobuf>=5,<6 (incompatible)
#                                 1.8.x  → protobuf>=4,<5   ← matches Airflow's
#                                                              4.25.6 constraint
#                               Airflow 2.10.5's constraint file pins
#                               protobuf==4.25.6 (a strict 4.x), so dbt 1.8 is
#                               the most recent line that resolves cleanly. We'll
#                               bump to 1.10 either when (a) Airflow's
#                               constraints relax — unlikely on 2.10's
#                               maintenance branch — or (b) we move back to
#                               Airflow 3, which uses protobuf 5+.
#   - dbt-postgres 1.8.x      : matches dbt-core minor (dbt convention)
#   - astronomer-cosmos 1.14.x: current stable; supports dbt 1.5–1.10 AND
#                               both Airflow 2.10+ and 3.x — survives both
#                               our planned future dbt bump AND a future
#                               Airflow re-bump without churning this file.
#
# Bump cadence: review on each OrcheStack semver bump. Patch versions
# float within minor (the "1.10.x" pattern); minor bumps are deliberate
# decisions that get a CHANGELOG entry.
#
# Why the constraint file: Airflow publishes a per-version constraint
# file that pins every transitive dep to a tested compatible version.
# Without it, pip's resolver explores the cross-product of our open
# pins (dbt 1.10.*, cosmos 1.14.*) × every cosmos transitive (jinja2,
# pendulum, sqlparse, ...) × Airflow's own peer constraints and gives
# up with `ResolutionTooDeep: 200000`. Airflow's constraint file is the
# canonical fix — it locks transitives, our direct pins decide dbt +
# cosmos, and the resolver converges in one pass.
ARG AIRFLOW_VERSION=2.10.5
ARG PYTHON_VERSION=3.12
ARG CONSTRAINT_URL="https://raw.githubusercontent.com/apache/airflow/constraints-${AIRFLOW_VERSION}/constraints-${PYTHON_VERSION}.txt"
RUN pip install --no-cache-dir --constraint "${CONSTRAINT_URL}" \
        "dbt-core>=1.8,<1.9" \
        "dbt-postgres>=1.8,<1.9" \
        "astronomer-cosmos>=1.14,<1.15"

# Build-time verification: container build fails if dbt or cosmos
# can't be imported. Catches version-pin breaks at build time, not
# at the operator's first DAG run.
#
# `dbt --version` exits with code 1 when an upstream newer version
# exists on PyPI — that's by design in dbt for end-user nag purposes,
# but for build-verification we just need "can we import these?",
# which Python imports answer cleanly without any update-check side
# effects.
RUN python -c "import dbt.version; import cosmos; \
    print(f'dbt-core OK: {dbt.version.__version__}'); \
    print(f'astronomer-cosmos OK: {cosmos.__version__}')"

# Re-set USER to airflow (the pip install above runs as root via the
# upstream image's entrypoint convention; runtime should drop back to
# the unprivileged airflow user the upstream image already created).
USER airflow
