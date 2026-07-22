import { expect, test } from "@playwright/test";

// Flow: native prefixed links. AppLink must render a real, correctly-prefixed
// <a href> (so cmd/middle-click and browser navigation work) AND intercept the
// click for SPA navigation without a full reload. jsdom validates neither the
// real anchor href nor real in-browser navigation, so this runs in Chromium.
test("a native AppLink anchor is prefixed and navigates in-browser", async ({ page }) => {
  await page.goto("/app/");

  const sessionLink = page.locator('a[href^="/app/sessions/"]').first();
  await expect(sessionLink).toBeVisible();

  const href = await sessionLink.getAttribute("href");
  expect(href).toMatch(/^\/app\/sessions\/[0-9a-f-]+$/);

  // A real browser click must land on the session detail route via the SPA
  // router (URL updates, detail renders) — not a 404 or a full document reload.
  let fullReload = false;
  page.once("load", () => {
    fullReload = true;
  });
  await sessionLink.click();

  await expect(page).toHaveURL(new RegExp("/app/sessions/[0-9a-f-]+$"));
  await expect(page.getByRole("heading", { name: "Session Detail" })).toBeVisible();
  expect(fullReload).toBe(false); // client-side navigation, not a document reload
});
