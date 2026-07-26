/* Service worker registration + offline-aware queue wiring.
 *
 * Safe to call on every app boot: if the browser does not support service
 * workers (older browsers, or jsdom in tests), the helpers no-op silently.
 */

import { createUploadQueue } from "./upload-queue.js";
import { TOKEN_STORAGE_KEY } from "./constants.js";

let cachedQueue = null;

function hasServiceWorker() {
  return (
    typeof navigator !== "undefined" &&
    typeof navigator.serviceWorker !== "undefined"
  );
}

function hasIndexedDb() {
  return typeof globalThis.indexedDB !== "undefined";
}

export function getUploadQueue() {
  if (cachedQueue || !hasIndexedDb()) {
    return cachedQueue;
  }
  cachedQueue = createUploadQueue();
  return cachedQueue;
}

export function resetUploadQueueForTests() {
  cachedQueue = null;
}

function storedToken() {
  try {
    return globalThis.localStorage?.getItem(TOKEN_STORAGE_KEY) || "";
  } catch {
    return "";
  }
}

export function droppedUploadsMessage(dropped) {
  const count = Array.isArray(dropped) ? dropped.length : 0;
  if (count <= 0) {
    return "";
  }
  return count === 1
    ? "1 queued capture could not be uploaded. Please capture it again."
    : `${count} queued captures could not be uploaded. Please capture them again.`;
}

function defaultSession() {
  // Boot-time default: a stored token but no proven owner identity. Because the
  // queue only drains under a session with a matching ownerId, a boot drain with
  // no owner is a safe no-op until the app supplies { token, ownerId }.
  return { token: storedToken(), ownerId: "" };
}

function readSession(getSession) {
  try {
    const session = getSession?.() || {};
    return { token: session.token || "", ownerId: session.ownerId || "" };
  } catch {
    return { token: "", ownerId: "" };
  }
}

function surfaceDroppedUploads(result, onDropped) {
  const dropped = result?.dropped || [];
  if (dropped.length === 0 || typeof onDropped !== "function") {
    return;
  }
  onDropped(dropped, result);
}

export function registerServiceWorker(
  scriptUrl = "/app/sw.js",
  {
    onUpdateReady = () => {},
    reloadWindow = () => window.location.reload(),
  } = {}
) {
  if (!hasServiceWorker()) {
    return Promise.resolve(null);
  }
  const serviceWorker = navigator.serviceWorker;
  const hadController = Boolean(serviceWorker.controller);
  if (hadController) {
    let reloadingForUpdate = false;
    serviceWorker.addEventListener?.(
      "controllerchange",
      () => {
        if (reloadingForUpdate) {
          return;
        }
        reloadingForUpdate = true;
        reloadWindow();
      },
      { once: true }
    );
  }
  return serviceWorker
    .register(scriptUrl, { updateViaCache: "none" })
    .then((registration) => {
      const advertisedWorkers = new WeakSet();
      const advertiseUpdatePrompt = (worker) => {
        // Only an already-controlled page can be running an older app shell.
        // First installs should continue to activate without a prompt.
        if (
          !hadController ||
          typeof worker?.postMessage !== "function" ||
          advertisedWorkers.has(worker)
        ) {
          return;
        }
        try {
          worker.postMessage({ type: "UPDATE_PROMPT_SUPPORTED" });
          advertisedWorkers.add(worker);
        } catch {
          // A worker can become redundant between inspection and postMessage.
        }
      };
      const notifyWhenWaiting = () => {
        const waiting = registration.waiting;
        if (!waiting || !hadController) {
          return;
        }
        advertiseUpdatePrompt(waiting);
        onUpdateReady(registration);
      };
      let observedInstalling = null;
      const observeInstalling = (installing) => {
        if (!installing || installing === observedInstalling) {
          return;
        }
        observedInstalling = installing;
        advertiseUpdatePrompt(installing);
        installing.addEventListener?.("statechange", () => {
          if (installing.state === "installed") {
            notifyWhenWaiting();
          }
        });
      };

      observeInstalling(registration.installing);
      notifyWhenWaiting();
      registration.addEventListener?.("updatefound", () => {
        observeInstalling(registration.installing);
      });
      try {
        Promise.resolve(registration.update?.()).catch(() => {});
      } catch {
        // Registration remains usable even if an explicit update check fails.
      }
      return registration;
    })
    .catch(() => null);
}

export function applyServiceWorkerUpdate(registration) {
  const waiting = registration?.waiting;
  if (!waiting || !hasServiceWorker()) {
    return false;
  }

  try {
    waiting.postMessage({ type: "SKIP_WAITING" });
    return true;
  } catch {
    return false;
  }
}

export function installOfflineRetry({
  queue = getUploadQueue(),
  getSession = defaultSession,
  onDropped = () => {},
} = {}) {
  if (!queue || typeof window === "undefined") {
    return () => {};
  }
  const handleOnline = () => {
    queue
      .drain(readSession(getSession))
      .then((result) => surfaceDroppedUploads(result, onDropped))
      .catch(() => {});
  };
  window.addEventListener("online", handleOnline);
  // Drain at boot too, in case the app was relaunched after going offline.
  queue
    .drain(readSession(getSession))
    .then((result) => surfaceDroppedUploads(result, onDropped))
    .catch(() => {});
  return () => window.removeEventListener("online", handleOnline);
}
