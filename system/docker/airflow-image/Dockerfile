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
# Version pins — picked to satisfy a four-way constraint we hit while
# reverting from Airflow 3 to 2.10:
#   (1) Airflow 2.10.5's constraint file pins protobuf==4.25.6
#   (2) dbt-core's protobuf requirement climbed minor-by-minor:
#         1.8.0  → protobuf >=4,<5   ← only patch that allows protobuf 4
#         1.8.1+ → protobuf >=5,<6
#         1.9.*  → protobuf >=5,<6 (or 6 for 1.9.9+)
#         1.10.* → protobuf >=6,<7
#   (3) dbt-common (a separate package dbt-core pulls in) has its OWN
#         protobuf ratchet — latest dbt-common 1.38 wants protobuf 6.
#   (4) cosmos must support BOTH our Airflow version AND our dbt version.
#
# Solution:
#   - dbt-core == 1.8.0          : exact pin — only patch that allows
#                                  protobuf 4. dbt 1.8.0 is the May-2024
#                                  release, the very first 1.8.
#   - dbt-postgres == 1.8.0      : matching dbt-core patch (dbt convention)
#   - dbt-common < 1.10          : floor below the dbt-common protobuf-5
#                                  bump; pip picks the highest <1.10 patch
#                                  that satisfies dbt-core 1.8.0's >=1.0.4
#                                  floor on dbt-common.
#   - dbt-adapters < 1.10        : same reasoning as dbt-common.
#   - astronomer-cosmos 1.8.*    : the pin from the ORIGINAL working
#                                  a6621e80 commit — known compatible with
#                                  dbt 1.8 and Airflow 2.10. Cosmos was
#                                  later bumped to 1.14 to fix an Airflow 3
#                                  import issue we no longer have.
#
# Constraint file: use Airflow's vanilla constraint file unmodified —
# it pins protobuf==4.25.6 which now matches dbt-core 1.8.0's >=4,<5
# requirement. No need to strip the protobuf pin (that workaround was
# for dbt 1.8.1+ which wants protobuf 5; we no longer ship those).
ARG AIRFLOW_VERSION=2.10.5
ARG PYTHON_VERSION=3.12
ARG CONSTRAINT_URL="https://raw.githubusercontent.com/apache/airflow/constraints-${AIRFLOW_VERSION}/constraints-${PYTHON_VERSION}.txt"

# libpq-dev is needed at build time for pip to build psycopg2 from sdist
# (something in dbt 1.8's transitive tree pulls in the non-binary
# psycopg2 sdist even though dbt-postgres itself depends on
# psycopg2-binary). Without libpq-dev the build dies with
# `pg_config executable not found`. The base airflow:2.10.5 image
# runs as the unprivileged `airflow` user; switch to root for apt-get
# then back to airflow for pip (matches the upstream image's own
# convention for build-time deps).
USER root
RUN apt-get update && \
    apt-get install -y --no-install-recommends libpq-dev && \
    rm -rf /var/lib/apt/lists/*
USER airflow

RUN pip install --no-cache-dir --constraint "${CONSTRAINT_URL}" \
        "dbt-core==1.8.0" \
        "dbt-postgres==1.8.0" \
        "dbt-common<1.10" \
        "dbt-adapters<1.10" \
        "astronomer-cosmos>=1.8,<1.9"

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
