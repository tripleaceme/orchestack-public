# OrcheStack — targeted Playwright scripts

Maintainer-runnable browser automations that exercise the specific
HTMX interactions that would be tedious to verify manually and that
benefit from automated assertion of the request-response cycle.

These are the artefacts Section 4.5 of the project report calls
"targeted Playwright drives" — the third of the four verification
layers, sitting between the smoke runbooks (which walk complete
operator flows) and the read-only database audits.

## When these are worth writing

- The flow's operator-visible symptom of failure is
  indistinguishable from success. The role-permissions bulk-save flow
  is the canonical example: manual clicks look the same whether the
  save succeeded or the closest-selector defect swallowed the payload.
  A Playwright script that asserts on request count + payload catches
  the difference.
- The flow depends on a specific HTMX-timing invariant that a manual
  operator cannot reliably reproduce (e.g. "the checkbox change fires
  no request UNTIL Save is clicked, then exactly one request goes out").
- The flow surfaces only under exact repeat clicks, not the varied
  clicks a manual smoke tester would perform.

## Scripts in this folder

| Script | Flow it verifies | Duration |
|---|---|---|
| [`role-permissions-bulk-save.spec.js`](role-permissions-bulk-save.spec.js) | Roles page edit-panel — ticks + Save fires exactly one POST; DB matches the ticks | ~15 s |

Future scripts belong here when they meet the "worth writing" bar
above. Do not add a Playwright script when a smoke runbook + a
verification query would already surface the same regression.

## Setup

One-time (per developer machine):

```bash
cd testing/PlayWright
npm install
npx playwright install chromium
```

## Running a script

Each script targets `http://localhost` by default and expects the
platform to be running from an `install-path.md` smoke. To point at a
different install:

```bash
ORCHESTACK_URL=http://localhost:8080 npx playwright test role-permissions-bulk-save.spec.js
```

Each script prints its assertions to stdout as it runs; a failing
assertion aborts the script with a non-zero exit code, which is the
signal a CI harness reads.

## Credentials

Scripts sign in as a maintainer-owned test user. Set two env vars
before running:

```bash
export ORCHESTACK_TEST_USER=your-admin-email@example.com
export ORCHESTACK_TEST_PASSWORD=your-password
```

Both must belong to a user with the `Admin` role — most of the
covered flows require Admin permission to complete.

Scripts should NEVER hard-code credentials. If a script would benefit
from a purpose-built test user rather than a personal account, create
one via `POST /api/dashboard/users/create` with the Admin role, set
its password, and cite that user's email in the script's setup block.
