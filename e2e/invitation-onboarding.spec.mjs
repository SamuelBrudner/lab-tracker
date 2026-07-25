import { expect, test } from "@playwright/test";

import {
  authEnabledBaseURL,
  bootstrapToken,
} from "../playwright.config.mjs";

const SOURCE_REVISION = "0123456789abcdef0123456789abcdef01234567";
const OWNER = {
  password: "E2e-owner-a-password-2026!",
  role: "admin",
  username: "e2e-offline-owner-a",
};

function responseData(response) {
  return response.json().then((payload) => payload.data);
}

async function registerOrLoginOwner(apiRequest) {
  const login = await apiRequest.post(`${authEnabledBaseURL}/auth/login`, {
    data: {
      password: OWNER.password,
      username: OWNER.username,
    },
  });
  if (login.ok()) {
    return responseData(login);
  }
  const registration = await apiRequest.post(
    `${authEnabledBaseURL}/auth/register`,
    {
      data: {
        bootstrap_token: bootstrapToken,
        password: OWNER.password,
        role: OWNER.role,
        username: OWNER.username,
      },
    }
  );
  expect(registration.status()).toBe(201);
  return responseData(registration);
}

test("admin invitation opens account setup and asks for Marion's full onboarding", async ({
  page,
  request: apiRequest,
}, testInfo) => {
  const owner = await registerOrLoginOwner(apiRequest);
  const invitedEmail = `e2e-marion-invite-${testInfo.retry}@example.org`;
  const invitationResponse = await apiRequest.post(
    `${authEnabledBaseURL}/auth/invitations`,
    {
      data: { email: invitedEmail, role: "admin" },
      headers: { Authorization: `Bearer ${owner.access_token}` },
    }
  );
  expect(invitationResponse.status()).toBe(201);
  const invitation = await responseData(invitationResponse);
  const inviteUrl = new URL(invitation.invite_url);
  const inviteToken = new URLSearchParams(inviteUrl.hash.slice(1)).get("invite");
  expect(inviteToken).toMatch(/^linv_/);
  expect(inviteUrl.search).toBe("");

  const requestUrls = [];
  page.on("request", (request) => requestUrls.push(request.url()));
  await page.goto(invitation.invite_url);

  await expect(
    page.getByRole("heading", { name: "Accept Invitation" })
  ).toBeVisible();
  await expect(page.getByLabel("Username")).toHaveValue(invitedEmail);
  await expect.poll(() => page.url()).not.toContain("#");
  expect(page.url()).not.toContain(inviteToken);
  expect(requestUrls.every((url) => !url.includes(inviteToken))).toBe(true);

  const invitedPassword = "E2e-invited-admin-password-2026!";
  await page.getByLabel("Password", { exact: true }).fill(invitedPassword);
  await page.getByLabel("Confirm password").fill(invitedPassword);
  const registerResponsePromise = page.waitForResponse(
    (response) =>
      new URL(response.url()).pathname === "/auth/register" &&
      response.request().method() === "POST"
  );
  await page.getByRole("button", { name: "Create account" }).click();
  const registerResponse = await registerResponsePromise;
  expect(registerResponse.status()).toBe(201);
  const invited = await responseData(registerResponse);
  expect(invited.user.role).toBe("admin");
  await expect(page.getByText(invitedEmail, { exact: true })).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "Set up your Lab Tracker" })
  ).toBeVisible();

  await page.getByLabel("Project name").fill("E2E Marion onboarding");
  await page
    .getByLabel("Short description")
    .fill("Disposable project for invitation onboarding verification.");
  const projectResponsePromise = page.waitForResponse(
    (response) =>
      new URL(response.url()).pathname === "/projects" &&
      response.request().method() === "POST"
  );
  await page.getByRole("button", { name: "Create project" }).click();
  const projectResponse = await projectResponsePromise;
  expect([200, 201]).toContain(projectResponse.status());
  const project = await responseData(projectResponse);

  await expect(
    page.getByRole("heading", { name: "Set your daily review timing" })
  ).toBeVisible();
  await page.getByLabel("Cadence").selectOption("10080");
  await page.getByLabel("Local run time").fill("09:30");
  await page.getByLabel("Time zone").fill("America/New_York");
  await expect(
    page.getByText(/email cues are unavailable because this host/i)
  ).toBeVisible();

  const scheduleResponsePromise = page.waitForResponse(
    (response) =>
      new URL(response.url()).pathname ===
        `/projects/${project.project_id}/graph-draft-batch-settings` &&
      response.request().method() === "PATCH"
  );
  await page.getByRole("button", { name: "Save cadence" }).click();
  const scheduleResponse = await scheduleResponsePromise;
  expect(scheduleResponse.status()).toBe(200);
  expect(scheduleResponse.request().postDataJSON()).toEqual(
    expect.objectContaining({
      cadence_minutes: 10080,
      email_notifications_enabled: false,
      notification_email: null,
      run_at_local_time: "09:30",
      timezone_name: "America/New_York",
      user_id: invited.user.user_id,
    })
  );
  await expect(page.getByText("Daily review schedule updated.")).toBeVisible();

  await expect(
    page.getByRole("heading", {
      name: "Seed your first questions with real context",
    })
  ).toBeVisible();
  await expect(
    page.getByLabel(/allow this context to be sent/i)
  ).toBeVisible();

  const setupText = await page.locator(".setup-page").innerText();
  expect(setupText).toContain(SOURCE_REVISION.slice(0, 12));
  expect(setupText).toContain(
    `lt project bind --project-id ${project.project_id} --yes`
  );
  expect(setupText).toContain(
    `lt hooks install --project ${project.project_id} --yes`
  );
  expect(setupText).toContain("lt setup init --install-skills --yes");
  expect(setupText).toContain("uv add");
  expect(setupText).toContain("lab_tracker_client import OK");
  expect(setupText).toContain("lt setup verify-mcp --expected-revision");
  expect(setupText).not.toContain("@main");
});
