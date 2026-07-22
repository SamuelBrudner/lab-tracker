import { expect, test } from "@playwright/test";

// Flow: service-worker offline shell. The SW must register, activate, precache
// the app shell, and serve it when the network is gone — the whole point of the
// PWA capture-first workflow, and something jsdom's stubbed SW cannot exercise.
test("the service worker serves the app shell offline", async ({ page, context }) => {
  await page.goto("/app/");

  // Wait for the service worker to control the page.
  await page.waitForFunction(async () => {
    if (!("serviceWorker" in navigator)) return false;
    const reg = await navigator.serviceWorker.getRegistration();
    return Boolean(reg && reg.active);
  }, null, { timeout: 15_000 });
  await page.evaluate(() => navigator.serviceWorker.ready);

  // Cut the network and reload: the shell must still come up from the SW cache.
  await context.setOffline(true);
  try {
    await page.reload({ waitUntil: "domcontentloaded" });
    await expect(page).toHaveTitle(/Lab Tracker/);
    await expect(page.locator("#app-root")).toBeAttached();
  } finally {
    await context.setOffline(false);
  }
});
