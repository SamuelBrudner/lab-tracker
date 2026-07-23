import { expect, test } from "@playwright/test";

import { prefixedBaseURL } from "../playwright.config.mjs";

// Flow: genuine non-root AppLink coverage through a prefix-stripping proxy.
// A normal click must remain same-document SPA navigation, while the exact href
// exposed to copy-link/open-in-new-tab must also load as a fresh document.
test("a prefixed AppLink works for SPA and copied-link navigation", async ({
  context,
  page,
}) => {
  await page.goto(`${prefixedBaseURL}/app/`);

  const sessionLink = page
    .locator('a[href^="/lab-tracker/app/sessions/"]')
    .first();
  await expect(sessionLink).toBeVisible();

  const href = await sessionLink.getAttribute("href");
  expect(href).toMatch(/^\/lab-tracker\/app\/sessions\/[0-9a-f-]+$/);
  const copiedUrl = new URL(href, prefixedBaseURL).href;

  await page.evaluate(() => {
    window.__labTrackerPrefixNavigationMarker = "same-document";
  });
  await sessionLink.click();

  await expect(page).toHaveURL(copiedUrl);
  await expect(page.getByRole("heading", { name: "Session Detail" })).toBeVisible();
  expect(
    await page.evaluate(() => window.__labTrackerPrefixNavigationMarker)
  ).toBe("same-document");

  const copiedPage = await context.newPage();
  await copiedPage.goto(copiedUrl);

  await expect(copiedPage).toHaveURL(copiedUrl);
  await expect(
    copiedPage.getByRole("heading", { name: "Session Detail" })
  ).toBeVisible();
});
