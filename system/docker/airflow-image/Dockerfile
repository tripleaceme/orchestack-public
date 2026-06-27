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
#   - dbt-core 1.10.x         : 1.10 is the line cosmos 1.14 supports
#   - dbt-postgres 1.10.x     : matches dbt-core minor (dbt convention)
#   - astronomer-cosmos 1.14.x: current stable; supports Airflow 2.10+
#                               AND 3.x, so the pin survives our planned
#                               future bump back to Airflow 3 without
#                               churning this Dockerfile.
#
# Bump cadence: review on each OrcheStack semver bump. Patch versions
# float within minor (the "1.10.x" pattern); minor bumps are deliberate
# decisions that get a CHANGELOG entry.
RUN pip install --no-cache-dir \
        "dbt-core>=1.10,<1.11" \
        "dbt-postgres>=1.10,<1.11" \
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
