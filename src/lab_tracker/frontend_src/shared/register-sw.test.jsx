import { afterEach, describe, expect, it, vi } from "vitest";

import { registerServiceWorker } from "./register-sw.js";

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
