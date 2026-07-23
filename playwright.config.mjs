import { defineConfig, devices } from "@playwright/test";

// Risk-focused browser lifecycle suite: only behavior that jsdom cannot validate
// (root/prefixed native links and offline/service-worker shell behavior), plus a
// Node-side multipart request check for the share-target fallback. Runs
// Chromium only, single-worker (service-worker/IndexedDB state must not bleed
// across tests), against two disposable app servers and one prefix proxy.
const CI = Boolean(process.env.CI);
const AUTH_DISABLED_PORT = process.env.E2E_PORT || "8177";
const AUTH_ENABLED_PORT = "8178";
const PREFIX_PROXY_PORT = process.env.E2E_PREFIX_PORT || "8179";
const BOOTSTRAP_TOKEN = "e2e-bootstrap-token";

export const authEnabledBaseURL = `http://127.0.0.1:${AUTH_ENABLED_PORT}`;
export const prefixedBaseURL = `http://127.0.0.1:${PREFIX_PROXY_PORT}/lab-tracker`;
export const bootstrapToken = BOOTSTRAP_TOKEN;

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  workers: 1,
  forbidOnly: CI,
  retries: CI ? 1 : 0,
  timeout: 60_000,
  reporter: CI ? [["list"], ["html", { open: "never" }]] : "list",
  use: {
    baseURL: `http://127.0.0.1:${AUTH_DISABLED_PORT}`,
    trace: "on-first-retry",
    screenshot: "only-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  // Both app servers receive mutations, and the proxy must target this run's
  // disposable upstream. A port collision must fail instead of attaching the
  // suite to an unrelated process.
  webServer: [
    {
      command: "node e2e/serve-e2e.mjs",
      env: { E2E_PORT: AUTH_DISABLED_PORT, E2E_AUTH_ENABLED: "false" },
      url: `http://127.0.0.1:${AUTH_DISABLED_PORT}/health`,
      reuseExistingServer: false,
      timeout: 120_000,
    },
    {
      command: "node e2e/serve-e2e.mjs",
      env: {
        E2E_PORT: AUTH_ENABLED_PORT,
        E2E_AUTH_ENABLED: "true",
        E2E_BOOTSTRAP_TOKEN: BOOTSTRAP_TOKEN,
      },
      url: `http://127.0.0.1:${AUTH_ENABLED_PORT}/health`,
      reuseExistingServer: false,
      timeout: 120_000,
    },
    {
      command: "node e2e/serve-prefix-proxy.mjs",
      env: {
        E2E_PREFIX_PORT: PREFIX_PROXY_PORT,
        E2E_UPSTREAM_PORT: AUTH_DISABLED_PORT,
      },
      url: `${prefixedBaseURL}/health`,
      reuseExistingServer: false,
      timeout: 120_000,
    },
  ],
});
