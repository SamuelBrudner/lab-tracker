import { expect, test } from "@playwright/test";

import {
  authEnabledBaseURL,
  bootstrapToken,
} from "../playwright.config.mjs";

const DRAFT_A_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const DRAFT_B_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";
const OPERATION_B_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbb01";
const OWNER_A = {
  password: "E2e-owner-a-password-2026!",
  role: "admin",
  username: "e2e-offline-owner-a",
};
const OWNER_B = {
  password: "E2e-owner-b-password-2026!",
  role: "viewer",
  username: "e2e-offline-owner-b",
};
const CREDENTIAL_SHAPED_KEYS = new Set([
  "accesstoken",
  "authorization",
  "authtoken",
  "bearertoken",
  "idtoken",
  "refreshtoken",
  "token",
]);

function deferred() {
  let resolve;
  const promise = new Promise((nextResolve) => {
    resolve = nextResolve;
  });
  return { promise, resolve };
}

function responsePath(response) {
  return new URL(response.url()).pathname;
}

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

async function createOwnerProject(apiRequest, owner) {
  const response = await apiRequest.post(`${authEnabledBaseURL}/projects`, {
    data: {
      client_capture_id: "e2e-offline-owner-project",
      description: "Project for identity-bound offline capture replay.",
      name: "E2E offline capture ownership",
    },
    headers: {
      Authorization: `Bearer ${owner.access_token}`,
    },
  });
  expect([200, 201]).toContain(response.status());
  return responseData(response);
}

async function signIn(page, account) {
  await expect(page.getByRole("heading", { name: "Sign In" })).toBeVisible();
  await page.getByLabel("Username").fill(account.username);
  await page.getByLabel("Password").fill(account.password);
  const loginResponse = page.waitForResponse(
    (response) =>
      responsePath(response) === "/auth/login" &&
      response.request().method() === "POST"
  );
  await page.getByRole("button", { name: "Sign in" }).click();
  const response = await loginResponse;
  expect(response.status()).toBe(200);
  const login = await responseData(response);
  await expect(page.getByText(account.username, { exact: true })).toBeVisible();
  return login;
}

async function signOut(page) {
  await page.getByRole("button", { name: "Sign out" }).click();
  await expect(page.getByRole("heading", { name: "Sign In" })).toBeVisible();
}

async function pendingUploads(page) {
  return page.evaluate(async () => {
    async function serializeIndexedDbValue(value) {
      if (value instanceof Blob) {
        return {
          __type: value instanceof File ? "File" : "Blob",
          ...(value instanceof File
            ? { lastModified: value.lastModified, name: value.name }
            : {}),
          size: value.size,
          text: await value.text(),
          type: value.type,
        };
      }
      if (Array.isArray(value)) {
        return Promise.all(value.map(serializeIndexedDbValue));
      }
      if (value && typeof value === "object") {
        const entries = await Promise.all(
          Object.entries(value).map(async ([key, nestedValue]) => [
            key,
            await serializeIndexedDbValue(nestedValue),
          ])
        );
        return Object.fromEntries(entries);
      }
      return value;
    }

    const db = await new Promise((resolve, reject) => {
      const request = indexedDB.open("lab-tracker-upload-queue", 1);
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error);
    });
    try {
      return await new Promise((resolve, reject) => {
        const transaction = db.transaction("pending", "readonly");
        const request = transaction.objectStore("pending").getAll();
        request.onsuccess = async () => {
          try {
            resolve(await Promise.all(request.result.map(serializeIndexedDbValue)));
          } catch (error) {
            reject(error);
          }
        };
        request.onerror = () => reject(request.error);
      });
    } finally {
      db.close();
    }
  });
}

function normalizeCredentialKey(key) {
  return key.replaceAll(/[^a-z0-9]/gi, "").toLowerCase();
}

function credentialKeyPaths(value, path = "$") {
  if (Array.isArray(value)) {
    return value.flatMap((nestedValue, index) =>
      credentialKeyPaths(nestedValue, `${path}[${index}]`)
    );
  }
  if (!value || typeof value !== "object") {
    return [];
  }
  return Object.entries(value).flatMap(([key, nestedValue]) => {
    const nestedPath = `${path}.${key}`;
    return [
      ...(CREDENTIAL_SHAPED_KEYS.has(normalizeCredentialKey(key))
        ? [nestedPath]
        : []),
      ...credentialKeyPaths(nestedValue, nestedPath),
    ];
  });
}

function expectQueuedCaptureWithoutCredentials(records, { accessToken, ownerId }) {
  expect(records).toHaveLength(1);
  expect(records[0]).toEqual(expect.objectContaining({ ownerId }));
  expect(credentialKeyPaths(records)).toEqual([]);
  expect(JSON.stringify(records)).not.toContain(accessToken);
}

test("an offline capture can only drain under its owner's live session", async ({
  page,
  request: apiRequest,
}) => {
  const ownerA = await registerOrLogin(apiRequest, OWNER_A);
  const ownerB = await registerOrLogin(apiRequest, OWNER_B);
  const project = await createOwnerProject(apiRequest, ownerA);

  let uploadTransportOffline = true;
  await page.route("**/notes/upload-file", async (route) => {
    if (uploadTransportOffline) {
      await route.abort("internetdisconnected");
      return;
    }
    await route.continue();
  });

  await page.goto(`${authEnabledBaseURL}/app/capture`);
  const browserOwnerA = await signIn(page, OWNER_A);
  await page
    .locator(".capture-context-fields select")
    .first()
    .selectOption(project.project_id);
  await page.getByLabel("Photo file").setInputFiles({
    buffer: Buffer.from("identity-bound-e2e-capture"),
    mimeType: "image/png",
    name: "owned-capture.png",
  });
  await page.getByRole("button", { name: "Save capture" }).click();

  await expect(
    page.getByText("Capture queued — will upload when you're back online.", {
      exact: true,
    })
  ).toBeVisible();
  await expect(
    page.getByText("1 capture queued offline", { exact: true })
  ).toBeVisible();
  const initiallyQueuedRecords = await pendingUploads(page);
  expectQueuedCaptureWithoutCredentials(initiallyQueuedRecords, {
    accessToken: browserOwnerA.access_token,
    ownerId: browserOwnerA.user.user_id,
  });

  const uploadRequests = [];
  page.on("request", (request) => {
    if (
      request.method() === "POST" &&
      new URL(request.url()).pathname === "/notes/upload-file"
    ) {
      uploadRequests.push(request);
    }
  });

  await signOut(page);
  await signIn(page, OWNER_B);
  uploadTransportOffline = false;
  await page.evaluate(() => window.dispatchEvent(new Event("online")));
  await page.waitForTimeout(400);

  expect(uploadRequests).toHaveLength(0);
  await expect(
    page.getByText("1 capture queued offline", { exact: true })
  ).toBeVisible();
  const ownerBQueuedRecords = await pendingUploads(page);
  expectQueuedCaptureWithoutCredentials(ownerBQueuedRecords, {
    accessToken: browserOwnerA.access_token,
    ownerId: browserOwnerA.user.user_id,
  });

  await signOut(page);
  const ownerUploadResponse = page.waitForResponse(
    (response) =>
      responsePath(response) === "/notes/upload-file" &&
      response.request().method() === "POST"
  );
  const returningOwnerA = await signIn(page, OWNER_A);
  const uploadResponse = await ownerUploadResponse;
  expect(uploadResponse.status()).toBe(201);
  expect(uploadResponse.request().headers().authorization).toBe(
    `Bearer ${returningOwnerA.access_token}`
  );
  const uploadedNote = await responseData(uploadResponse);
  expect(uploadedNote.created_by_user_id).toBe(returningOwnerA.user.user_id);
  expect(uploadedNote.created_by_user_id).not.toBe(ownerB.user.user_id);

  await expect(
    page.getByText("1 capture queued offline", { exact: true })
  ).toHaveCount(0);
  expect(await pendingUploads(page)).toEqual([]);
  await page.evaluate(() => window.dispatchEvent(new Event("online")));
  await page.waitForTimeout(400);
  expect(uploadRequests).toHaveLength(1);
});

test("a delayed graph response cannot replace or mutate the current route", async ({
  page,
}) => {
  const seenA = deferred();
  const seenB = deferred();
  const releaseA = deferred();
  const releaseB = deferred();
  const mutationPaths = [];

  page.on("request", (request) => {
    const pathname = new URL(request.url()).pathname;
    if (request.method() !== "GET" && pathname.includes("/graph-drafts/")) {
      mutationPaths.push(pathname);
    }
  });
  await page.route("**/graph-drafts/**", async (route) => {
    const request = route.request();
    const pathname = new URL(request.url()).pathname;
    if (request.method() === "GET" && pathname === `/graph-drafts/${DRAFT_A_ID}`) {
      seenA.resolve();
      await releaseA.promise;
    } else if (
      request.method() === "GET" &&
      pathname === `/graph-drafts/${DRAFT_B_ID}`
    ) {
      seenB.resolve();
      await releaseB.promise;
    }
    await route.continue();
  });

  await page.goto(`/app/graph-drafts/${DRAFT_A_ID}`);
  await seenA.promise;
  await page.evaluate((draftBId) => {
    window.history.pushState({}, "", `/app/graph-drafts/${draftBId}`);
    window.dispatchEvent(new PopStateEvent("popstate"));
  }, DRAFT_B_ID);
  await seenB.promise;

  await expect(page).toHaveURL(new RegExp(`/app/graph-drafts/${DRAFT_B_ID}$`));
  await expect(page.getByText("Loading...", { exact: true })).toBeVisible();
  await expect(page.getByText("Draft A summary.", { exact: true })).toHaveCount(0);
  await expect(page.getByText("Draft B summary.", { exact: true })).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "Accept", exact: true })
  ).toHaveCount(0);

  const draftBResponse = page.waitForResponse(
    (response) => responsePath(response) === `/graph-drafts/${DRAFT_B_ID}`
  );
  releaseB.resolve();
  expect((await draftBResponse).status()).toBe(200);
  await expect(page.getByText("Draft B summary.", { exact: true })).toBeVisible();
  await expect(
    page.locator(".review-proposal-text", {
      hasText: "Question proposed by draft B",
    })
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Accept", exact: true })
  ).toBeEnabled();

  const lateDraftAResponse = page.waitForResponse(
    (response) => responsePath(response) === `/graph-drafts/${DRAFT_A_ID}`
  );
  releaseA.resolve();
  const lateResponse = await lateDraftAResponse;
  expect(lateResponse.status()).toBe(200);
  await lateResponse.finished();
  await page.waitForTimeout(100);
  await expect(page.getByText("Draft B summary.", { exact: true })).toBeVisible();
  await expect(page.getByText("Draft A summary.", { exact: true })).toHaveCount(0);

  await page.getByText("Edit this proposal", { exact: true }).click();
  await page
    .locator(".review-edit textarea")
    .first()
    .fill("Question proposed by draft B, edited.");
  const saveResponse = page.waitForResponse(
    (response) =>
      response.request().method() === "PATCH" &&
      responsePath(response) ===
        `/graph-drafts/${DRAFT_B_ID}/operations/${OPERATION_B_ID}`
  );
  await page.getByRole("button", { name: "Save edit", exact: true }).click();
  expect((await saveResponse).status()).toBe(200);
  await expect(
    page.getByText("Graph draft operation updated.", { exact: true })
  ).toBeVisible();

  const acceptResponse = page.waitForResponse(
    (response) =>
      response.request().method() === "PATCH" &&
      responsePath(response) ===
        `/graph-drafts/${DRAFT_B_ID}/operations/${OPERATION_B_ID}`
  );
  await page.getByRole("button", { name: "Accept", exact: true }).click();
  expect((await acceptResponse).status()).toBe(200);
  await expect(page.getByText("accepted", { exact: true })).toBeVisible();

  expect(mutationPaths).toEqual([
    `/graph-drafts/${DRAFT_B_ID}/operations/${OPERATION_B_ID}`,
    `/graph-drafts/${DRAFT_B_ID}/operations/${OPERATION_B_ID}`,
  ]);
});
