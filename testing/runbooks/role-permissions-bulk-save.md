# Smoke — Roles page bulk-save flow

Verifies the Roles page edit-panel behaviour that the report's §4.4
identifies as the flow that surfaced the HTMX `closest`-selector
defect — ticks across multiple service rows, single Save Changes
click, one POST request out, and the database ends up with exactly
the rows the operator ticked.

This runbook exists specifically because a manual click-through cannot
distinguish "I clicked and nothing saved" from "I clicked, the save
succeeded, but the re-rendered fragment does not reflect my change."
The verification queries below make that distinction visible.

**Applies to:** v0.1.1
**Duration:** ~2 min.

## Setup

- OrcheStack is installed, reachable, and signed into as an operator
  whose role holds the `Admin` grant.
- At least one non-Admin role exists (Analyst and Engineer both come
  seeded). If neither exists, create one via
  `POST /api/dashboard/roles/create` before running this procedure.
- The dashboard's browser DevTools network tab is open so you can
  count the number of outbound requests.

## Steps

1. **Navigate to the Roles page.** Path: sidebar → Configuration →
   Roles. Expected: the page renders with a dropdown labelled
   `SHOW ROLE` and a permission matrix for the currently-selected
   role, one row per service (9 rows on a fully-configured install).

2. **From the SHOW ROLE dropdown, select a non-Admin role**
   (Analyst or a newly-created role). Expected: the permission matrix
   re-renders for the selected role via HTMX (one GET request in the
   network tab). If NO request fires, the closest-selector defect from
   §4.4 has regressed — the Save function is bound to a stale select
   element and the whole flow is broken.

3. **Click `Edit`.** Expected: the matrix's checkboxes become
   interactive; a `Save changes` button appears at the bottom.

4. **Clear the network tab (right-click → Clear).** This isolates the
   next actions' traffic for the counting step below.

5. **Tick three checkboxes across three different service rows.**
   Suggested combination: `can_start` on Airflow, `can_use` on dbt,
   `can_force_stop` on Airbyte. Expected: each tick is a purely visual
   change with ZERO outbound requests. If any HTMX POST fires during
   ticking, the deferred-save discipline described in §4.5 has
   regressed — the panel is auto-saving per tick, which is the bug
   the bulk-save redesign fixed.

6. **Click `Save changes`.** Expected: exactly ONE POST request goes
   out (to `/api/dashboard/roles/{id}/bulk-set-permissions`) with a
   form payload containing the ticked checkboxes' `name` attributes.

7. **Observe the re-rendered matrix.** Expected: the three ticks the
   operator made are still visible; the matrix has re-rendered as an
   HTMX fragment swap.

## Verification

### 1. Confirm the three explicit rows landed in the database

```sql
SELECT service_name, can_start, can_use, can_force_stop, can_edit_config
FROM platform.role_permissions
WHERE role_id = (SELECT id FROM platform.roles WHERE name = 'Analyst')
ORDER BY service_name;
```
Expected: rows for `airflow`, `airbyte`, `dbt` with the flags the
operator ticked. Any wildcard `*` row for the role has been REPLACED
with these explicit rows (the wildcard-to-explicit rewrite on save
described in the §4.2.4 correction).

### 2. Confirm no wildcard row survived the save

```sql
SELECT COUNT(*)
FROM platform.role_permissions
WHERE role_id = (SELECT id FROM platform.roles WHERE name = 'Analyst')
  AND service_name = '*';
```
Expected: `0`. If the count is `1`, the wildcard was not converted
into explicit rows — the operator's mental model (the checkboxes they
see) no longer matches the database (which still holds one wildcard
row that expands into 9 ticks).

### 3. Confirm the audit trail recorded the save

```sql
SELECT id, event_type, target, actor_username,
       details->'permissions' AS payload_permissions
FROM platform.audit_log
WHERE event_type = 'role_permissions_updated'
ORDER BY id DESC LIMIT 1;
```
Expected: one row, most-recent, with the target set to the role name
and the `payload_permissions` payload containing the same three
service-permission pairs the operator ticked.

## Known caveats

- The wildcard-to-explicit rewrite is destructive to the wildcard's
  compactness — after any save, the role holds N per-service rows
  instead of 1 wildcard row. This is intentional and documented in
  the §4.2.4 correction, but a maintainer running this smoke needs to
  know the wildcard will not come back on its own.
- Firefox's DevTools sometimes buffers requests for a few hundred
  milliseconds; the "exactly one POST" assertion in Step 6 is safer
  to check in Chromium-family browsers where the network panel is
  strictly ordered.
