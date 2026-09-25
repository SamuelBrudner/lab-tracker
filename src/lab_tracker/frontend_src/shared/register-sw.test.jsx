import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

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
  // Fake timers keep the hourly update check from outliving a test, and the
  // signal removes each registration's foreground listener.
  let watch;

  beforeEach(() => {
    vi.useFakeTimers();
    watch = new AbortController();
  });

  afterEach(() => {
    watch.abort();
    vi.useRealTimers();
  });

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
      registerServiceWorker("/app/sw.js", { onUpdateReady, reloadWindow, signal: watch.signal })
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

    await registerServiceWorker("/app/sw.js", { onUpdateReady, signal: watch.signal });
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

    await registerServiceWorker("/app/sw.js", { onUpdateReady, signal: watch.signal });

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

    await registerServiceWorker("/app/sw.js", { onUpdateReady, signal: watch.signal });

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

    await expect(
      registerServiceWorker(undefined, { signal: watch.signal })
    ).resolves.toBe(registration);
    expect(applyServiceWorkerUpdate(registration)).toBe(false);
  });

  describe("rechecking after boot", () => {
    let visibility;

    beforeEach(() => {
      visibility = "visible";
      Object.defineProperty(document, "visibilityState", {
        configurable: true,
        get: () => visibility,
      });
    });

    afterEach(() => {
      delete document.visibilityState;
    });

    async function registerControlledPage(options = {}) {
      const registration = {
        addEventListener: vi.fn(),
        installing: null,
        update: vi.fn(async () => {}),
        waiting: null,
      };
      setServiceWorker({
        addEventListener: vi.fn(),
        controller: {},
        register: vi.fn(async () => registration),
      });
      await registerServiceWorker("/app/sw.js", { signal: watch.signal, ...options });
      return registration;
    }

    function setVisibility(state) {
      visibility = state;
      document.dispatchEvent(new Event("visibilitychange"));
    }

    it("checks again when a resumed app returns to the foreground", async () => {
      const registration = await registerControlledPage();
      expect(registration.update).toHaveBeenCalledTimes(1);

      vi.advanceTimersByTime(5 * 60 * 1000);
      setVisibility("hidden");
      expect(registration.update).toHaveBeenCalledTimes(1);

      setVisibility("visible");
      expect(registration.update).toHaveBeenCalledTimes(2);
    });

    it("does not recheck on every quick app switch", async () => {
      const registration = await registerControlledPage();

      vi.advanceTimersByTime(30 * 1000);
      setVisibility("hidden");
      setVisibility("visible");

      expect(registration.update).toHaveBeenCalledTimes(1);
    });

    it("checks hourly while open on screen, never in the background", async () => {
      const registration = await registerControlledPage();

      vi.advanceTimersByTime(60 * 60 * 1000);
      expect(registration.update).toHaveBeenCalledTimes(2);

      visibility = "hidden";
      vi.advanceTimersByTime(3 * 60 * 60 * 1000);
      expect(registration.update).toHaveBeenCalledTimes(2);
    });

    it("stops checking once its signal is aborted", async () => {
      const registration = await registerControlledPage();

      watch.abort();
      vi.advanceTimersByTime(2 * 60 * 60 * 1000);
      setVisibility("visible");

      expect(registration.update).toHaveBeenCalledTimes(1);
    });

    it("offers the usual reload prompt for an update found in the foreground", async () => {
      const onUpdateReady = vi.fn();
      const registration = await registerControlledPage({ onUpdateReady });
      const [, onUpdateFound] = registration.addEventListener.mock.calls.find(
        ([event]) => event === "updatefound"
      );
      const stateChangeListeners = {};
      const installing = {
        state: "installing",
        addEventListener: vi.fn((event, listener) => {
          stateChangeListeners[event] = listener;
        }),
        postMessage: vi.fn(),
      };
      registration.update.mockImplementationOnce(async () => {
        registration.installing = installing;
        onUpdateFound();
      });

      // The server was redeployed while the app sat in the background.
      vi.advanceTimersByTime(10 * 60 * 1000);
      setVisibility("visible");
      await Promise.resolve();
      registration.waiting = installing;
      installing.state = "installed";
      stateChangeListeners.statechange();

      expect(installing.postMessage).toHaveBeenCalledWith({ type: "UPDATE_PROMPT_SUPPORTED" });
      expect(onUpdateReady).toHaveBeenCalledWith(registration);
    });
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

    expect(queue.drain).toHaveBeenCalledWith({
      token: "fresh-token",
      ownerId: "owner-1",
      authEnabled: true,
    });
    expect(onDropped).toHaveBeenCalledWith(dropped, {
      dropped,
      uploaded: [],
      stillQueued: [],
    });

    cleanup();
  });

  it("logs a failed drain instead of swallowing it", async () => {
    const failure = new TypeError("drain exploded");
    const queue = { drain: vi.fn(async () => Promise.reject(failure)) };
    const consoleError = vi.spyOn(console, "error").mockImplementation(() => {});

    const cleanup = installOfflineRetry({
      getSession: () => ({ token: "t", ownerId: "owner-1" }),
      queue,
    });
    await vi.waitFor(() =>
      expect(consoleError).toHaveBeenCalledWith("Offline capture retry failed:", failure)
    );

    consoleError.mockClear();
    window.dispatchEvent(new Event("online"));
    await vi.waitFor(() =>
      expect(consoleError).toHaveBeenCalledWith("Offline capture retry failed:", failure)
    );

    cleanup();
  });

  it("forwards an auth-disabled session so owner-only drains can run", async () => {
    const queue = {
      drain: vi.fn(async () => ({ dropped: [], uploaded: [], stillQueued: [] })),
    };

    const cleanup = installOfflineRetry({
      getSession: () => ({ token: "", ownerId: "local-user", authEnabled: false }),
      queue,
    });
    await Promise.resolve();
    window.dispatchEvent(new Event("online"));
    await Promise.resolve();

    expect(queue.drain).toHaveBeenCalledTimes(2);
    for (const call of queue.drain.mock.calls) {
      expect(call[0]).toEqual({ token: "", ownerId: "local-user", authEnabled: false });
    }

    cleanup();
  });

  it("treats a session without an explicit auth-disabled flag as auth-enabled", async () => {
    const queue = {
      drain: vi.fn(async () => ({ dropped: [], uploaded: [], stillQueued: [] })),
    };

    const cleanup = installOfflineRetry({
      getSession: () => ({ token: "", ownerId: "owner-1" }),
      queue,
    });
    await Promise.resolve();

    expect(queue.drain).toHaveBeenCalledWith({ token: "", ownerId: "owner-1", authEnabled: true });

    cleanup();
  });

  it("formats dropped upload messages", () => {
    expect(droppedUploadsMessage([{ id: 1 }])).toContain("1 queued capture");
    expect(droppedUploadsMessage([{ id: 1 }, { id: 2 }])).toContain("2 queued captures");
    expect(droppedUploadsMessage([])).toBe("");
  });
});
