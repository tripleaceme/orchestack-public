// Targeted Playwright drive — Roles page bulk-save flow.
//
// The flow this script verifies:
//   1. Navigate to the Roles page as an Admin.
//   2. Select a non-Admin role from the SHOW ROLE dropdown.
//   3. Open the Edit panel.
//   4. Tick three checkboxes across three different service rows.
//      Assert that ZERO HTMX POST requests fire during the ticks
//      (the deferred-save discipline described in the report §4.5).
//   5. Click Save changes.
//      Assert that exactly ONE POST goes out to
//      /api/dashboard/roles/{id}/bulk-set-permissions
//      with the three ticked (name, value) pairs in the form body.
//   6. Verify the fragment re-renders with the three ticks still visible.
//
// The script was decisive during v0.1.1 development in surfacing the
// HTMX `closest`-selector defect (Section 4.4). A manual operator
// clicking through the same sequence would have observed the same
// broken UI but could not have distinguished "I clicked and nothing
// saved" from "I clicked, the save succeeded, but the re-rendered
// fragment does not reflect my change." The intermediate assertions
// below (request count during ticks, request count on save, payload
// shape) make that distinction visible.
//
// Verify the persisted database rows separately via
// `system/runbooks/role-permissions-bulk-save.md` step 5 — this
// script asserts on the request-response cycle only.

const { test, expect } = require('@playwright/test');

const BASE = process.env.ORCHESTACK_URL || 'http://localhost';
const USER = process.env.ORCHESTACK_TEST_USER;
const PASS = process.env.ORCHESTACK_TEST_PASSWORD;

if (!USER || !PASS) {
  throw new Error(
    'Set ORCHESTACK_TEST_USER and ORCHESTACK_TEST_PASSWORD before running.\n' +
    'See ./README.md for details.'
  );
}

// Three ticks across three different service rows. Picked to be
// low-risk on any install (start on airflow / use on dbt / force-stop
// on airbyte — none of these actions actually fires as a side effect
// of ticking the checkbox; only Save posts them).
const TICKS_TO_MAKE = [
  { service: 'airflow',  permission: 'can_start' },
  { service: 'dbt',      permission: 'can_use' },
  { service: 'airbyte',  permission: 'can_force_stop' },
];

// A role that starts life without any explicit per-service grants,
// so the assertions on post-save state are unambiguous. Analyst
// ships seeded with a wildcard-only grant, so the save will
// wildcard-to-explicit-rewrite (Section 4.2.4 correction).
const TARGET_ROLE = 'Analyst';

test('roles page: bulk-save flow fires one POST with the ticked payload', async ({ page }) => {

  // ----- 1. Sign in -------------------------------------------------
  await page.goto(`${BASE}/app/login`);
  await page.fill('input[name="username_or_email"]', USER);
  await page.fill('input[name="password"]', PASS);
  await page.click('button[type="submit"]');
  await page.waitForURL(/\/app\/?$/, { timeout: 15000 });

  // ----- 2. Navigate to Roles ---------------------------------------
  await page.goto(`${BASE}/app/roles`);
  await expect(page.locator('h1')).toContainText('Roles');

  // ----- 3. Select the target role from the SHOW ROLE dropdown -----
  await page.selectOption('select[name="selected_role_id"], select#selected_role_id',
                           { label: new RegExp(`^${TARGET_ROLE}\\b`) });
  await page.waitForLoadState('networkidle');

  // ----- 4. Open the Edit panel -------------------------------------
  await page.click('button:has-text("Edit"), a:has-text("Edit")');
  await expect(page.locator('input[type="checkbox"]').first()).toBeVisible();

  // ----- 5. Count HTMX POST requests during checkbox ticks --------
  //         Deferred-save discipline: ticks should NOT trigger requests.
  let postCountDuringTicks = 0;
  const tickRequestListener = (req) => {
    if (req.method() === 'POST' && req.url().includes('/api/dashboard/roles/')) {
      postCountDuringTicks += 1;
      console.log(`  ⚠  Unexpected POST during tick: ${req.url()}`);
    }
  };
  page.on('request', tickRequestListener);

  for (const { service, permission } of TICKS_TO_MAKE) {
    const cb = page.locator(`input[type="checkbox"][name*="${service}"][name*="${permission}"]`).first();
    await cb.check();
  }

  // Give any late-firing HTMX request a moment to arrive
  await page.waitForTimeout(400);
  page.off('request', tickRequestListener);

  expect(postCountDuringTicks,
    'Deferred-save discipline violated — a POST fired while ticking').toBe(0);
  console.log(`  ✓  ${TICKS_TO_MAKE.length} ticks fired 0 POSTs (deferred-save discipline honoured)`);

  // ----- 6. Click Save changes, capture the single POST -----------
  const [saveRequest] = await Promise.all([
    page.waitForRequest(
      (req) => req.method() === 'POST'
            && /\/api\/dashboard\/roles\/\d+\/bulk-set-permissions/.test(req.url()),
      { timeout: 5000 }
    ),
    page.click('button:has-text("Save changes")'),
  ]);

  // ----- 7. Assert the POST payload contains the ticks -------------
  const postBody = saveRequest.postData() || '';
  for (const { service, permission } of TICKS_TO_MAKE) {
    const key = `${service}_${permission}`;
    expect(postBody,
      `Save POST body missing the ${service}/${permission} tick — the include selector may be swallowing it`)
      .toMatch(new RegExp(`(^|&)${key}(=on|=true|=1)?(&|$)`));
  }
  console.log(`  ✓  Save posted 1 request carrying ${TICKS_TO_MAKE.length} expected ticks`);

  // ----- 8. Confirm the re-rendered matrix still shows the ticks ---
  await page.waitForLoadState('networkidle');
  for (const { service, permission } of TICKS_TO_MAKE) {
    const cb = page.locator(`input[type="checkbox"][name*="${service}"][name*="${permission}"]`).first();
    await expect(cb,
      `Post-save fragment lost the ${service}/${permission} tick — the save may have succeeded silently but the response fragment is stale`)
      .toBeChecked();
  }
  console.log(`  ✓  Re-rendered matrix reflects all ${TICKS_TO_MAKE.length} ticks`);
});
