import { expect, test } from "@playwright/test";

import {
  authEnabledBaseURL,
  bootstrapToken,
} from "../playwright.config.mjs";

const OWNER = {
  password: "E2e-owner-a-password-2026!",
  role: "admin",
  username: "e2e-invitation-owner",
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

test("an invitation accepts one account without exposing its token in requests", async ({
  page,
  request: apiRequest,
}, testInfo) => {
  const owner = await registerOrLoginOwner(apiRequest);
  const invitedEmail = `e2e-invite-${testInfo.retry}@example.org`;
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

  await expect(page.getByRole("heading", { name: "Accept Invitation" })).toBeVisible();
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
});
