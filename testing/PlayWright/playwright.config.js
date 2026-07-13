// Playwright config. Every knob here is intentional — do not paste the
// default `npx playwright init` config, it turns on parallel workers
// and multiple browser targets, both of which fight each other against
// a single-instance OrcheStack install.
const { defineConfig } = require('@playwright/test');

module.exports = defineConfig({
  testDir: '.',
  // One test at a time — the OrcheStack instance under test is a
  // single deployment, and the bulk-save spec mutates the Analyst
  // role. Parallel runs would race on the same DB row.
  workers: 1,
  fullyParallel: false,
  // Fail fast in CI. Locally, keep `retries: 0` so a flaky assertion
  // surfaces on the first run rather than being masked.
  retries: process.env.CI ? 1 : 0,
  reporter: [['list']],
  use: {
    // Chromium only — the flows targeted here are HTMX request-response
    // assertions, not visual regressions, so cross-browser is overkill.
    // Add firefox / webkit projects below only if a specific spec needs
    // them.
    headless: true,
    ignoreHTTPSErrors: true,
    trace: 'retain-on-failure',
  },
});
