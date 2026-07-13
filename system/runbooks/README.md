# OrcheStack — smoke runbooks

Maintainer-runnable verification procedures for the operator-facing
flows OrcheStack ships. These are the artefacts Section 4.5 of the
project report calls "end-to-end smoke procedures" — the second layer
of the four-layer verification strategy (health checks → smoke →
targeted Playwright → read-only DB audit).

Each runbook is structured into three sections, in order:

1. **Setup** — the platform state assumed before the procedure runs,
   plus any host-side prerequisites (Docker running, ports free,
   fresh install vs upgrade).
2. **Steps** — numbered operator actions with the expected outcome at
   each step. An outcome that does not match aborts the procedure and
   is the observation the maintainer records.
3. **Verification** — after the operator-visible flow completes, the
   maintainer runs one or more SQL queries against the platform's
   metadata database (`orchestack_db`) to confirm the persisted state
   matches what the UI displayed. This layer catches the class of bug
   described in §4.4 where the UI's defensive `None`-handling hides
   an underlying data-shape disagreement.

## When to run these

- **Before cutting a release** — the maintainer walks the highest-risk
  procedures against a clean-slate install of the release candidate.
- **After merging a change that touches a flow's code path** — the
  matching runbook is the fastest way to confirm the change did not
  break operator-visible behaviour.
- **When triaging an operator-reported issue** — the runbook whose
  flow the operator hit is the reproducible baseline against which the
  reported behaviour is compared.

## Prerequisites shared by every runbook

- Docker 24+ with Compose v2.20+.
- Ports 80 and 443 free on the host.
- 8 GB RAM available.
- macOS on Apple silicon or a Linux x86_64 host with a multi-arch-
  compatible Docker Desktop / Engine.
- `psql` client on the host (or willingness to `docker exec` into
  `orchestack-postgres` for the verification queries).

Individual runbooks add their own prerequisites in their **Setup**
section.

## The runbooks

| File | Flow it verifies | Duration |
|---|---|---|
| [`install-path.md`](install-path.md) | Download → checksum → `docker compose up` → dashboard reachable at http://localhost | ~2 min |
| [`openmetadata-first-boot.md`](openmetadata-first-boot.md) | The longest cross-component dependency chain — pre-start hook → ES healthy → OM server healthy → post-start hook → operator can open | ~10 min on first boot |
| [`role-permissions-bulk-save.md`](role-permissions-bulk-save.md) | Roles page edit-panel flow — ticks across services + single Save; the flow that surfaced the HTMX `closest`-selector defect described in §4.4 | ~2 min |
| [`pipeline-manual-run.md`](pipeline-manual-run.md) | Create pipeline → trigger manual → observe DAG progress on runs page | ~5 min |

## Where the other flows are covered

Runbooks in the format above exist for the flows the report calls out
by name. Other operator flows are covered by two complementary
artefacts:

- **`_smoke_test.py`** at `system/dashboard/_smoke_test.py` — an
  executable Python integration test that stubs the orchestrator client
  and drives every dashboard route + HTMX fragment + action endpoint.
  Runnable as `python _smoke_test.py` from `system/dashboard/`; runs
  in under a second and asserts the routes wire up correctly.
- **`report/evidence/testing-evidence.md`** at the report side — the
  narrative record of every verification cycle from M1 through the
  v0.1.1 stabilisation, milestone by milestone, with the bugs found
  and lessons learned. Not runbook-shaped, but the primary evidence
  document the report cites.

The report language in §4.5 was written before all of the runbooks
were formalised into this folder; the intent has always been that
critical flows would sit here and other coverage would come from the
Python smoke test and the narrative evidence. This README makes that
partition explicit.

## Adding a new runbook

1. Copy [`install-path.md`](install-path.md) as a template (it has the
   simplest Setup / Steps / Verification shape).
2. Fill in the flow's specific state, actions, and audit queries.
3. Include the SQL queries verbatim so the runbook is copy-paste
   executable — do not paraphrase.
4. Add the runbook to the table in this README.

Runbooks should be short: a well-scoped procedure fits in 60-100 lines
of markdown. If a runbook grows beyond that, it is likely covering
more than one flow and should be split.
