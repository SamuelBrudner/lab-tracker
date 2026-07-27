import { afterEach, describe, expect, it, vi } from "vitest";

import {
  applyServiceWorkerUpdate,
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

  it("notifies when an update is already waiting for a controlled page", async () => {
    const update = vi.fn(async () => {});
    const waiting = { postMessage: vi.fn() };
    const registration = {
      addEventListener: vi.fn(),
      update,
      waiting,
    };
    const listeners = {};
    const serviceWorker = {
      addEventListener: vi.fn((event, listener) => {
        listeners[event] = listener;
      }),
      controller: {},
      register: vi.fn(async () => registration),
    };
    const onUpdateReady = vi.fn();
    const reloadWindow = vi.fn();
    setServiceWorker(serviceWorker);

    await expect(
      registerServiceWorker("/app/sw.js", { onUpdateReady, reloadWindow })
    ).resolves.toBe(registration);

    expect(serviceWorker.register).toHaveBeenCalledWith("/app/sw.js", { updateViaCache: "none" });
    expect(update).toHaveBeenCalledTimes(1);
    expect(waiting.postMessage).toHaveBeenCalledWith({
      type: "UPDATE_PROMPT_SUPPORTED",
    });
    expect(onUpdateReady).toHaveBeenCalledWith(registration);

    listeners.controllerchange();
    listeners.controllerchange();

    expect(reloadWindow).toHaveBeenCalledTimes(1);
  });

  it("advertises prompt support to a newly installing worker", async () => {
    const updateFoundListeners = {};
    const stateChangeListeners = {};
    const installing = {
      state: "installing",
      addEventListener: vi.fn((event, listener) => {
        stateChangeListeners[event] = listener;
      }),
      postMessage: vi.fn(),
    };
    const registration = {
      addEventListener: vi.fn((event, listener) => {
        updateFoundListeners[event] = listener;
      }),
      installing: null,
      update: vi.fn(async () => {}),
      waiting: null,
    };
    const serviceWorker = {
      controller: {},
      register: vi.fn(async () => registration),
    };
    const onUpdateReady = vi.fn();
    setServiceWorker(serviceWorker);

    await registerServiceWorker("/app/sw.js", { onUpdateReady });
    registration.installing = installing;
    updateFoundListeners.updatefound();

    expect(installing.postMessage).toHaveBeenCalledWith({
      type: "UPDATE_PROMPT_SUPPORTED",
    });

    registration.waiting = installing;
    installing.state = "installed";
    stateChangeListeners.statechange();

    expect(onUpdateReady).toHaveBeenCalledWith(registration);
  });

  it("observes a worker that was already installing when registration resolved", async () => {
    const stateChangeListeners = {};
    const installing = {
      state: "installing",
      addEventListener: vi.fn((event, listener) => {
        stateChangeListeners[event] = listener;
      }),
      postMessage: vi.fn(),
    };
    const registration = {
      addEventListener: vi.fn(),
      installing,
      update: vi.fn(async () => {}),
      waiting: null,
    };
    const serviceWorker = {
      controller: {},
      register: vi.fn(async () => registration),
    };
    const onUpdateReady = vi.fn();
    setServiceWorker(serviceWorker);

    await registerServiceWorker("/app/sw.js", { onUpdateReady });

    expect(installing.postMessage).toHaveBeenCalledWith({
      type: "UPDATE_PROMPT_SUPPORTED",
    });

    registration.waiting = installing;
    installing.state = "installed";
    stateChangeListeners.statechange();

    expect(onUpdateReady).toHaveBeenCalledWith(registration);
  });

  it("does not announce or suppress activation on first install", async () => {
    const waiting = { postMessage: vi.fn() };
    const registration = {
      addEventListener: vi.fn(),
      update: vi.fn(async () => {}),
      waiting,
    };
    const serviceWorker = {
      addEventListener: vi.fn(),
      controller: null,
      register: vi.fn(async () => {
        // clients.claim() may establish a controller while registration is
        // resolving; the boot-time snapshot must still classify this as an
        // initial install.
        serviceWorker.controller = {};
        return registration;
      }),
    };
    const onUpdateReady = vi.fn();
    setServiceWorker(serviceWorker);

    await registerServiceWorker("/app/sw.js", { onUpdateReady });

    expect(waiting.postMessage).not.toHaveBeenCalled();
    expect(onUpdateReady).not.toHaveBeenCalled();
    expect(serviceWorker.addEventListener).not.toHaveBeenCalled();
  });

  it("asks a waiting worker to activate", () => {
    setServiceWorker({ addEventListener: vi.fn() });
    const waiting = { postMessage: vi.fn() };

    expect(applyServiceWorkerUpdate({ waiting })).toBe(true);
    expect(waiting.postMessage).toHaveBeenCalledWith({ type: "SKIP_WAITING" });
  });

  it("does nothing when there is no waiting worker", () => {
    setServiceWorker({ addEventListener: vi.fn() });

    expect(applyServiceWorkerUpdate(null)).toBe(false);
  });

  it("treats registration and activation failures as non-fatal", async () => {
    const registration = {
      addEventListener: vi.fn(),
      update: vi.fn(() => {
        throw new Error("offline");
      }),
      waiting: {
        postMessage: vi.fn(() => {
          throw new Error("redundant");
        }),
      },
    };
    setServiceWorker({
      addEventListener: vi.fn(),
      controller: {},
      register: vi.fn(async () => registration),
    });

    await expect(registerServiceWorker()).resolves.toBe(registration);
    expect(applyServiceWorkerUpdate(registration)).toBe(false);
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
