# OrcheStack Airflow image.
#
# Apache Airflow 3 with dbt-core, dbt-postgres, and astronomer-cosmos
# preinstalled so the operator's first DAG can drive dbt with per-model
# task granularity without any runtime pip-install.
#
# Build context: this folder (system/docker/airflow-image/).
#
# Locally:
#   make image-airflow            # produces tripleaceme/orchestack-airflow:dev
#
# Tag bumping policy: bump alongside the rest of the OrcheStack release
# tags (the release.yml docker-publish matrix sets both :latest and the
# semver tag on every image at once).

FROM apache/airflow:3.2-python3.12

LABEL org.opencontainers.image.title="orchestack-airflow" \
      org.opencontainers.image.description="Apache Airflow 3 with dbt-core, dbt-postgres, and astronomer-cosmos preinstalled for OrcheStack" \
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
#   - dbt-core 1.10.x   : current minor at OrcheStack v0.1.0 cut
#   - dbt-postgres 1.10.x : matches dbt-core minor (required by dbt convention)
#   - astronomer-cosmos 1.8.x : current minor; explicitly supports Airflow 3.0+
#
# Bump cadence: review on each OrcheStack semver bump. Patch versions
# float within minor (the "1.10.x" pattern); minor bumps are deliberate
# decisions that get a CHANGELOG entry.
RUN pip install --no-cache-dir \
        "dbt-core>=1.10,<1.11" \
        "dbt-postgres>=1.10,<1.11" \
        "astronomer-cosmos>=1.8,<1.9"

# Build-time verification: container build fails if any binary is
# missing. Catches version-pin breaks at build time, not at the
# operator's first DAG run.
RUN dbt --version && \
    python -c "import cosmos; print(f'cosmos OK: {cosmos.__version__}')"

# Re-set USER to airflow (the pip install above runs as root via the
# upstream image's entrypoint convention; runtime should drop back to
# the unprivileged airflow user the upstream image already created).
USER airflow
