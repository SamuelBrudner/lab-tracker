import { afterEach, describe, expect, it, vi } from "vitest";

import {
  disablePushNotifications,
  enablePushNotifications,
  getPushNotificationState,
  rebindExistingPushSubscription,
  urlBase64ToUint8Array,
} from "./push-notifications.js";
import { apiResponse, installFetchMock } from "../test/utils.js";

const originalServiceWorker = navigator.serviceWorker;

function setServiceWorker(serviceWorker) {
  Object.defineProperty(navigator, "serviceWorker", {
    configurable: true,
    value: serviceWorker,
  });
}

afterEach(() => {
  setServiceWorker(originalServiceWorker);
  vi.unstubAllGlobals();
});

function installPushGlobals(registration, permission = "granted") {
  vi.stubGlobal("PushManager", class PushManager {});
  vi.stubGlobal("Notification", {
    permission,
    requestPermission: vi.fn(async () => permission),
  });
  setServiceWorker({ ready: Promise.resolve(registration) });
}

describe("push notification subscription lifecycle", () => {
  it("subscribes with the VAPID key and binds the browser endpoint on the server", async () => {
    const subscription = {
      endpoint: "https://fcm.googleapis.com/fcm/send/example",
      expirationTime: null,
      toJSON: () => ({
        endpoint: "https://fcm.googleapis.com/fcm/send/example",
        expirationTime: null,
        keys: { auth: "auth-key", p256dh: "browser-key" },
      }),
    };
    const registration = {
      pushManager: {
        getSubscription: vi.fn(async () => null),
        subscribe: vi.fn(async () => subscription),
      },
    };
    installPushGlobals(registration);
    const publicKey = btoa(String.fromCharCode(4, ...new Array(64).fill(1)))
      .replace(/\+/g, "-")
      .replace(/\//g, "_")
      .replace(/=+$/, "");
    let postedBody = null;
    installFetchMock([
      {
        match: "/notifications/push/config",
        response: apiResponse({
          application_server_key: publicKey,
          enabled: true,
          secure_context_required: true,
        }),
      },
      {
        match: "/notifications/push/subscriptions",
        method: "POST",
        response: (request) => {
          postedBody = JSON.parse(request.init.body);
          return apiResponse({ subscription_id: "subscription-1" }, 201);
        },
      },
    ]);

    await enablePushNotifications({ token: "token-1" });

    expect(registration.pushManager.subscribe).toHaveBeenCalledWith({
      applicationServerKey: expect.any(Uint8Array),
      userVisibleOnly: true,
    });
    expect(postedBody).toEqual({
      endpoint: subscription.endpoint,
      expiration_time_ms: null,
      keys: { auth: "auth-key", p256dh: "browser-key" },
    });
  });

  it("revokes the server binding before unsubscribing the browser endpoint", async () => {
    const callOrder = [];
    const subscription = {
      endpoint: "https://fcm.googleapis.com/fcm/send/example",
      unsubscribe: vi.fn(async () => {
        callOrder.push("browser");
        return true;
      }),
    };
    installPushGlobals({
      pushManager: { getSubscription: vi.fn(async () => subscription) },
    });
    installFetchMock([
      {
        match: "/notifications/push/unsubscribe",
        method: "POST",
        response: () => {
          callOrder.push("server");
          return apiResponse(true);
        },
      },
    ]);

    await disablePushNotifications({ token: "token-1" });

    expect(callOrder).toEqual(["server", "browser"]);
  });

  it("reports an existing browser subscription without prompting", async () => {
    const subscription = {
      endpoint: "https://fcm.googleapis.com/fcm/send/example",
      expirationTime: null,
      toJSON: () => ({
        endpoint: "https://fcm.googleapis.com/fcm/send/example",
        expirationTime: null,
        keys: { auth: "auth-key", p256dh: "browser-key" },
      }),
    };
    installPushGlobals({
      pushManager: { getSubscription: vi.fn(async () => subscription) },
    });
    installFetchMock([
      {
        match: "/notifications/push/config",
        response: apiResponse({ application_server_key: "key", enabled: true }),
      },
      {
        match: "/notifications/push/subscriptions",
        method: "POST",
        response: apiResponse({ subscription_id: "subscription-1" }, 201),
      },
    ]);

    await expect(getPushNotificationState({ token: "token-1" })).resolves.toEqual({
      configured: true,
      permission: "granted",
      secure: true,
      subscribed: true,
      supported: true,
    });
  });

  it("rebinds a surviving browser endpoint to the current authenticated user", async () => {
    const subscription = {
      endpoint: "https://fcm.googleapis.com/fcm/send/shared",
      expirationTime: null,
      toJSON: () => ({
        endpoint: "https://fcm.googleapis.com/fcm/send/shared",
        expirationTime: null,
        keys: { auth: "auth-key", p256dh: "browser-key" },
      }),
    };
    installPushGlobals({
      pushManager: { getSubscription: vi.fn(async () => subscription) },
    });
    let authorization = "";
    installFetchMock([
      {
        match: "/notifications/push/config",
        response: apiResponse({ application_server_key: "key", enabled: true }),
      },
      {
        match: "/notifications/push/subscriptions",
        method: "POST",
        response: (request) => {
          authorization = request.init.headers.Authorization;
          return apiResponse({ subscription_id: "subscription-1" }, 201);
        },
      },
    ]);

    await expect(
      rebindExistingPushSubscription({ token: "current-user-token" })
    ).resolves.toBe(true);
    expect(authorization).toBe("Bearer current-user-token");
  });

  it("decodes URL-safe base64 VAPID keys", () => {
    expect(Array.from(urlBase64ToUint8Array("BAECAw"))).toEqual([4, 1, 2, 3]);
  });
});
