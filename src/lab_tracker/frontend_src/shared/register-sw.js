/* Service worker registration + offline-aware queue wiring.
 *
 * Safe to call on every app boot: if the browser does not support service
 * workers (older browsers, or jsdom in tests), the helpers no-op silently.
 */

import { createUploadQueue } from "./upload-queue.js";
import { TOKEN_STORAGE_KEY } from "./constants.js";

let cachedQueue = null;

// A home-screen app is usually resumed from memory rather than relaunched, so
// a check made only at boot can leave a phone on an old app shell for days.
// Recheck whenever the app returns to the foreground (at most this often)...
const FOREGROUND_UPDATE_MIN_INTERVAL_MS = 60 * 1000;
// ...and periodically while it stays open on screen.
const OPEN_APP_UPDATE_INTERVAL_MS = 60 * 60 * 1000;

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
  return { token: storedToken(), ownerId: "", authEnabled: true };
}

function readSession(getSession) {
  try {
    const session = getSession?.() || {};
    return {
      token: session.token || "",
      ownerId: session.ownerId || "",
      // Only an explicit `false` (the server reported auth disabled) lifts the
      // live-token requirement; anything else keeps the auth-enabled guard.
      authEnabled: session.authEnabled !== false,
    };
  } catch {
    return { token: "", ownerId: "", authEnabled: true };
  }
}

function surfaceDroppedUploads(result, onDropped) {
  const dropped = result?.dropped || [];
  if (dropped.length === 0 || typeof onDropped !== "function") {
    return;
  }
  onDropped(dropped, result);
}

function checkForUpdate(registration) {
  try {
    Promise.resolve(registration.update?.()).catch(() => {});
  } catch {
    // Registration remains usable even if an explicit update check fails.
  }
}

// Checks now, then again on return to the foreground and while visible. An
// update found this way reaches the page through the registration's existing
// updatefound wiring, so it gets the same "Reload to update" prompt.
function watchForUpdates(registration, signal) {
  if (signal?.aborted) {
    return;
  }
  let lastCheck = Date.now();
  checkForUpdate(registration);
  const checkIfDue = () => {
    if (
      document.visibilityState !== "visible" ||
      Date.now() - lastCheck < FOREGROUND_UPDATE_MIN_INTERVAL_MS
    ) {
      return;
    }
    lastCheck = Date.now();
    checkForUpdate(registration);
  };
  document.addEventListener("visibilitychange", checkIfDue, { signal });
  const timer = setInterval(checkIfDue, OPEN_APP_UPDATE_INTERVAL_MS);
  signal?.addEventListener("abort", () => clearInterval(timer), { once: true });
}

export function registerServiceWorker(
  scriptUrl = "/app/sw.js",
  {
    onUpdateReady = () => {},
    reloadWindow = () => window.location.reload(),
    // Aborting stops the recurring update checks (the app shell never does).
    signal,
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
      watchForUpdates(registration, signal);
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
  const drain = () => {
    queue
      .drain(readSession(getSession))
      .then((result) => surfaceDroppedUploads(result, onDropped))
      .catch((error) => {
        // Queued captures stay queued for the next retry; make the failure
        // visible rather than silently holding them.
        // eslint-disable-next-line no-console
        console.error("Offline capture retry failed:", error);
      });
  };
  window.addEventListener("online", drain);
  // Drain at boot too, in case the app was relaunched after going offline.
  drain();
  return () => window.removeEventListener("online", drain);
}
