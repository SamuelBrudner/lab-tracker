import { expect, test } from "@playwright/test";

import {
  authEnabledBaseURL,
  bootstrapToken,
} from "../playwright.config.mjs";

const OWNER = {
  password: "E2e-owner-a-password-2026!",
  role: "admin",
  username: "e2e-offline-owner-a",
};

async function responseData(response) {
  return (await response.json()).data;
}

async function registerOrLogin(apiRequest, account) {
  const login = await apiRequest.post(`${authEnabledBaseURL}/auth/login`, {
    data: {
      password: account.password,
      username: account.username,
    },
  });
  if (login.ok()) {
    return responseData(login);
  }
  const registration = await apiRequest.post(`${authEnabledBaseURL}/auth/register`, {
    data: {
      bootstrap_token: account.role === "admin" ? bootstrapToken : undefined,
      password: account.password,
      role: account.role,
      username: account.username,
    },
  });
  expect(registration.status()).toBe(201);
  return responseData(registration);
}

async function signIn(page, account) {
  await expect(page.getByRole("heading", { name: "Sign In" })).toBeVisible();
  await page.getByLabel("Username").fill(account.username);
  await page.getByLabel("Password").fill(account.password);
  const loginResponse = page.waitForResponse(
    (response) =>
      new URL(response.url()).pathname === "/auth/login" &&
      response.request().method() === "POST"
  );
  await page.getByRole("button", { name: "Sign in" }).click();
  expect((await loginResponse).status()).toBe(200);
  await expect(page.getByText(account.username, { exact: true })).toBeVisible();
}

test("an ongoing-project member checkpoints, aligns manually, and completes a forward capture", async ({
  page,
  request,
}) => {
  const projectResponse = await request.post("/projects", {
    data: {
      client_capture_id: "e2e-member-onboarding-project-v1",
      description: "A scientific project already in motion before Lab Tracker adoption.",
      name: "E2E ongoing project orientation",
    },
  });
  expect([200, 201]).toContain(projectResponse.status());
  const project = await responseData(projectResponse);

  await page.goto(`/app/projects/${project.project_id}/onboarding`);
  await expect(
    page.getByRole("heading", { name: /Start tracking E2E ongoing project orientation/ })
  ).toBeVisible();

  await page
    .getByLabel("What output or decision are you working toward now?")
    .fill("Choose the decisive control cohort");
  await page.getByLabel("Question 1").fill("Does the pilot effect persist in controls?");
  await page
    .getByLabel("What recent result or context matters most?")
    .fill("The pilot effect reproduced in two independent sessions");
  await page
    .getByLabel("What is the next move?")
    .fill("Run the matched control comparison");

  const checkpointResponse = page.waitForResponse(
    (response) =>
      new URL(response.url()).pathname ===
        `/projects/${project.project_id}/member-onboarding/checkpoint` &&
      response.request().method() === "PUT"
  );
  await page.getByRole("button", { name: "Save tracking checkpoint" }).click();
  expect((await checkpointResponse).ok()).toBe(true);
  await expect(
    page.getByText("Strongest recent context", { exact: true }).locator("..")
  ).toContainText("The pilot effect reproduced in two independent sessions");

  await page.getByRole("button", { name: "Align questions manually" }).click();
  await page.getByLabel("Resolution for question 1").selectOption("checkpoint_only");
  const alignmentResponse = page.waitForResponse(
    (response) =>
      new URL(response.url()).pathname ===
        `/projects/${project.project_id}/member-onboarding/manual-alignment` &&
      response.request().method() === "PUT"
  );
  await page.getByRole("button", { name: "Save each resolution" }).click();
  expect((await alignmentResponse).ok()).toBe(true);

  await expect(page.getByRole("heading", { name: "Project-now brief" })).toBeVisible();
  await expect(page.getByLabel("Current-state question map")).toContainText(
    "Does the pilot effect persist in controls?"
  );
  await expect(page.getByLabel("Current-state question map")).toContainText(
    "Checkpoint only"
  );

  await page.getByRole("button", { name: "Make the first capture" }).click();
  await expect(page).toHaveURL(/\/app\/capture\?/);
  await expect(page.getByText("Tracking checkpoint attached", { exact: true })).toBeVisible();
  await expect(
    page.locator('[aria-labelledby="capture-context-title"]').getByRole("combobox").first()
  ).toBeDisabled();
  await page.getByLabel("Message or hint").fill("Control cohort is scheduled for Friday");

  const captureResponse = page.waitForResponse(
    (response) =>
      new URL(response.url()).pathname === "/notes" &&
      response.request().method() === "POST"
  );
  await page.getByRole("button", { name: "Save capture" }).click();
  expect((await captureResponse).status()).toBe(201);

  await expect(page).toHaveURL(`/app/projects/${project.project_id}/onboarding`);
  await expect(
    page.locator(".member-onboarding-heading-actions").getByText("Orientation complete", {
      exact: true,
    })
  ).toBeVisible();
  await expect(page.getByRole("button", { name: "Finish in workspace" })).toBeEnabled();
});

test("an authenticated contributor reviews AI alignment before an owner commits it", async ({
  browser,
  page,
  request: apiRequest,
}, testInfo) => {
  const contributorAccount = {
    password: "E2e-onboarding-contributor-password-2026!",
    role: "viewer",
    username: `e2e-onboarding-contributor-${testInfo.retry}`,
  };
  const owner = await registerOrLogin(apiRequest, OWNER);
  const contributor = await registerOrLogin(apiRequest, contributorAccount);

  const projectResponse = await apiRequest.post(`${authEnabledBaseURL}/projects`, {
    data: {
      client_capture_id: `e2e-auth-member-onboarding-${testInfo.retry}`,
      description: "An auth-enabled project for owner-gated onboarding review.",
      name: `E2E authenticated onboarding ${testInfo.retry}`,
    },
    headers: { Authorization: `Bearer ${owner.access_token}` },
  });
  expect([200, 201]).toContain(projectResponse.status());
  const project = await responseData(projectResponse);

  const membershipResponse = await apiRequest.post(
    `${authEnabledBaseURL}/projects/${project.project_id}/members`,
    {
      data: {
        role: "contributor",
        user_id: contributor.user.user_id,
      },
      headers: { Authorization: `Bearer ${owner.access_token}` },
    }
  );
  expect([200, 201]).toContain(membershipResponse.status());

  await page.goto(`${authEnabledBaseURL}/app/projects/${project.project_id}/onboarding`);
  await signIn(page, contributorAccount);
  await expect(
    page.getByRole("heading", { name: /Start tracking E2E authenticated onboarding/ })
  ).toBeVisible();

  await page
    .getByLabel("What output or decision are you working toward now?")
    .fill("Select the confirmatory assay configuration");
  await page.getByLabel("Question 1").fill("Does the blinded assay reproduce the pilot effect?");
  await page
    .getByLabel("What recent result or context matters most?")
    .fill("Two pilot runs agreed after correcting the calibration offset");
  await page
    .getByLabel("What is the next move?")
    .fill("Run the blinded confirmatory cohort");
  await page.getByRole("button", { name: "Save tracking checkpoint" }).click();
  await expect(page.getByText("Saved", { exact: true }).first()).toBeVisible();

  await page.getByLabel(/I consent to send this checkpoint/).check();
  const alignmentResponse = page.waitForResponse(
    (response) =>
      new URL(response.url()).pathname ===
        `/projects/${project.project_id}/member-onboarding/ai-alignment` &&
      response.request().method() === "POST"
  );
  await page.getByRole("button", { name: "Suggest question alignments" }).click();
  expect((await alignmentResponse).status()).toBe(200);

  const proposal = page.locator(".member-onboarding-ai-review .item").first();
  await expect(proposal).toContainText("Does the blinded assay reproduce the pilot effect?");
  await proposal.getByRole("button", { name: "Accept" }).click();
  await expect(proposal.getByText("accepted", { exact: true })).toBeVisible();

  const submitResponse = page.waitForResponse(
    (response) =>
      /\/graph-drafts\/[^/]+\/submit$/.test(new URL(response.url()).pathname) &&
      response.request().method() === "POST"
  );
  await page.getByRole("button", { name: "Submit each decision" }).click();
  expect((await submitResponse).status()).toBe(200);
  await expect(
    page.locator(".member-onboarding-heading-actions").getByText("Awaiting project owner", {
      exact: true,
    })
  ).toBeVisible();

  const ownerContext = await browser.newContext({ baseURL: authEnabledBaseURL });
  try {
    const ownerPage = await ownerContext.newPage();
    await ownerPage.goto("/app");
    await signIn(ownerPage, OWNER);

    const ownerQueue = ownerPage.locator(".member-onboarding-owner-queue");
    await expect(ownerQueue).toContainText("1 member map awaits your commit");
    await expect(ownerQueue).toContainText("1 accepted change");
    await ownerQueue.getByRole("button", { name: "Review and commit" }).click();

    await expect(ownerPage.getByRole("heading", { name: "Review" })).toBeVisible();
    await expect(
      ownerPage.locator(".review-proposal-actions").getByText("accepted", { exact: true })
    ).toBeVisible();
    await ownerPage
      .getByLabel("Commit message")
      .fill("Commit the contributor's reviewed onboarding map");
    const commitResponse = ownerPage.waitForResponse(
      (response) =>
        /\/graph-drafts\/[^/]+\/commit$/.test(new URL(response.url()).pathname) &&
        response.request().method() === "POST"
    );
    await ownerPage.getByRole("button", { name: "Commit accepted changes" }).click();
    expect((await commitResponse).status()).toBe(200);

    await expect(ownerPage.getByText("Graph draft committed.", { exact: true })).toBeVisible();
    await expect(
      ownerPage.locator(".review-proposal-actions").getByText("applied", { exact: true })
    ).toBeVisible();
    await expect(
      ownerPage.getByRole("button", { name: "Commit accepted changes" })
    ).toBeDisabled();
  } finally {
    await ownerContext.close();
  }

  const queueResponse = await apiRequest.get(
    `${authEnabledBaseURL}/projects/${project.project_id}/member-onboarding/owner-queue`,
    { headers: { Authorization: `Bearer ${owner.access_token}` } }
  );
  expect(queueResponse.status()).toBe(200);
  expect(await responseData(queueResponse)).toEqual([]);
});
