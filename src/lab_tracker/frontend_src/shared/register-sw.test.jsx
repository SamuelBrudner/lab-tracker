import { afterEach, describe, expect, it, vi } from "vitest";

import {
  droppedUploadsMessage,
  installOfflineRetry,
  registerServiceWorker,
} from "./register-sw.js";

const originalServiceWorker = navigator.serviceWorker;

function setServiceWorker(serviceWorker) {
  Object.defineProperty(navigator, "serviceWorker", {
    configurable: true,
    value: serviceWorker,
  });
}

afterEach(() => {
  vi.restoreAllMocks();
  setServiceWorker(originalServiceWorker);
});

describe("registerServiceWorker", () => {
  it("no-ops when service workers are unavailable", async () => {
    setServiceWorker(undefined);

    await expect(registerServiceWorker()).resolves.toBeNull();
  });

  it("checks for updates and reloads once when an existing worker is replaced", async () => {
    const update = vi.fn(async () => {});
    const registration = { update };
    const listeners = {};
    const serviceWorker = {
      addEventListener: vi.fn((event, listener) => {
        listeners[event] = listener;
      }),
      controller: {},
      register: vi.fn(async () => registration),
    };
    const reloadWindow = vi.fn();
    setServiceWorker(serviceWorker);

    await expect(
      registerServiceWorker("/app/sw.js", { reloadWindow })
    ).resolves.toBe(registration);

    expect(serviceWorker.register).toHaveBeenCalledWith("/app/sw.js", { updateViaCache: "none" });
    expect(update).toHaveBeenCalledTimes(1);
    expect(serviceWorker.addEventListener).toHaveBeenCalledWith(
      "controllerchange",
      expect.any(Function),
      { once: true }
    );

    listeners.controllerchange();
    listeners.controllerchange();

    expect(reloadWindow).toHaveBeenCalledTimes(1);
  });

  it("does not reload on first install when no worker controls the page yet", async () => {
    const serviceWorker = {
      addEventListener: vi.fn(),
      controller: null,
      register: vi.fn(async () => ({ update: vi.fn(async () => {}) })),
    };
    setServiceWorker(serviceWorker);

    await registerServiceWorker("/app/sw.js", { reloadWindow: vi.fn() });

    expect(serviceWorker.addEventListener).not.toHaveBeenCalled();
  });
});

describe("installOfflineRetry", () => {
  it("drains with the current token and surfaces dropped uploads", async () => {
    const dropped = [{ id: 1, rejectedStatus: 422 }];
    const queue = {
      drain: vi.fn(async () => ({ dropped, uploaded: [], stillQueued: [] })),
    };
    const onDropped = vi.fn();

    const cleanup = installOfflineRetry({
      getSession: () => ({ token: "fresh-token", ownerId: "owner-1" }),
      queue,
      onDropped,
    });

    await Promise.resolve();

    expect(queue.drain).toHaveBeenCalledWith({ token: "fresh-token", ownerId: "owner-1" });
    expect(onDropped).toHaveBeenCalledWith(dropped, {
      dropped,
      uploaded: [],
      stillQueued: [],
    });

    cleanup();
  });

  it("formats dropped upload messages", () => {
    expect(droppedUploadsMessage([{ id: 1 }])).toContain("1 queued capture");
    expect(droppedUploadsMessage([{ id: 1 }, { id: 2 }])).toContain("2 queued captures");
    expect(droppedUploadsMessage([])).toBe("");
  });
});
