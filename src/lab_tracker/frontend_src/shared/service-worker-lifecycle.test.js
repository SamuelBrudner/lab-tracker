import { afterEach, describe, expect, it, vi } from "vitest";

async function loadServiceWorker({ cacheInstall = Promise.resolve() } = {}) {
  const listeners = {};
  const cache = {
    addAll: vi.fn(() => cacheInstall),
  };
  const worker = {
    addEventListener: vi.fn((type, listener) => {
      listeners[type] = listener;
    }),
    clients: {
      claim: vi.fn(async () => {}),
    },
    location: {
      origin: "https://lab.example.org",
    },
    skipWaiting: vi.fn(async () => {}),
  };
  const cacheStorage = {
    delete: vi.fn(async () => true),
    keys: vi.fn(async () => []),
    open: vi.fn(async () => cache),
  };

  vi.stubGlobal("self", worker);
  vi.stubGlobal("caches", cacheStorage);
  vi.resetModules();
  await import("../../frontend/sw.js");

  return { cache, cacheStorage, listeners, worker };
}

function dispatchInstall(listeners) {
  let installPromise;
  listeners.install({
    waitUntil: (promise) => {
      installPromise = promise;
    },
  });
  return installPromise;
}

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.resetModules();
});

describe("service worker update lifecycle", () => {
  it("auto-activates when no prompt-capable client handshakes", async () => {
    vi.useFakeTimers();
    const { listeners, worker } = await loadServiceWorker();

    const installPromise = dispatchInstall(listeners);
    await vi.advanceTimersByTimeAsync(1500);
    await installPromise;

    expect(worker.skipWaiting).toHaveBeenCalledTimes(1);
  });

  it("waits when a prompt-capable client handshakes before the grace period", async () => {
    vi.useFakeTimers();
    const { listeners, worker } = await loadServiceWorker();

    const installPromise = dispatchInstall(listeners);
    listeners.message({
      data: { type: "UPDATE_PROMPT_SUPPORTED" },
      waitUntil: vi.fn(),
    });
    await vi.advanceTimersByTimeAsync(1500);
    await installPromise;

    expect(worker.skipWaiting).not.toHaveBeenCalled();
  });

  it("honors a handshake received while cache population is still pending", async () => {
    vi.useFakeTimers();
    let finishCaching;
    const cacheInstall = new Promise((resolve) => {
      finishCaching = resolve;
    });
    const { listeners, worker } = await loadServiceWorker({ cacheInstall });

    const installPromise = dispatchInstall(listeners);
    await vi.advanceTimersByTimeAsync(1500);
    listeners.message({
      data: { type: "UPDATE_PROMPT_SUPPORTED" },
      waitUntil: vi.fn(),
    });
    finishCaching();
    await installPromise;

    expect(worker.skipWaiting).not.toHaveBeenCalled();
  });

  it("extends the activation message until skipWaiting settles", async () => {
    vi.useFakeTimers();
    const { listeners, worker } = await loadServiceWorker();
    const waitUntil = vi.fn();

    listeners.message({
      data: { type: "SKIP_WAITING" },
      waitUntil,
    });

    expect(worker.skipWaiting).toHaveBeenCalledTimes(1);
    expect(waitUntil).toHaveBeenCalledTimes(1);
    await waitUntil.mock.calls[0][0];
  });

  it("ignores unknown messages", async () => {
    vi.useFakeTimers();
    const { listeners, worker } = await loadServiceWorker();
    const waitUntil = vi.fn();

    listeners.message({ data: { type: "OTHER" }, waitUntil });

    expect(worker.skipWaiting).not.toHaveBeenCalled();
    expect(waitUntil).not.toHaveBeenCalled();
  });

  it("fails installation instead of activating an incomplete cache", async () => {
    vi.useFakeTimers();
    const cacheError = new Error("cache unavailable");
    const { listeners, worker } = await loadServiceWorker({
      cacheInstall: Promise.reject(cacheError),
    });

    const installPromise = dispatchInstall(listeners);
    const rejection = expect(installPromise).rejects.toThrow("cache unavailable");
    await vi.advanceTimersByTimeAsync(1500);
    await rejection;

    expect(worker.skipWaiting).not.toHaveBeenCalled();
  });
});
