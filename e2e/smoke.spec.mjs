import { expect, test } from "@playwright/test";

// Harness smoke: the disposable server boots and serves the app shell in a real
// browser. If this fails, the webServer/boot wiring is broken, not a flow.
test("app shell loads in a real browser", async ({ page }) => {
  await page.goto("/app/");
  await expect(page).toHaveTitle(/Lab Tracker/);
  await expect(page.locator("#app-root")).toBeAttached();
});
