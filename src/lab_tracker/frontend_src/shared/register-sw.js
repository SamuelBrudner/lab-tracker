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

function readToken(getToken) {
  try {
    return getToken?.() || "";
  } catch {
    return "";
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
  { reloadOnControllerChange = true, reloadWindow = () => window.location.reload() } = {}
) {
  if (!hasServiceWorker()) {
    return Promise.resolve(null);
  }
  const serviceWorker = navigator.serviceWorker;
  const shouldReloadOnUpdate = reloadOnControllerChange && Boolean(serviceWorker.controller);
  let reloadingForUpdate = false;
  if (shouldReloadOnUpdate) {
    serviceWorker.addEventListener(
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
      registration.update?.().catch(() => {});
      return registration;
    })
    .catch(() => null);
}

export function installOfflineRetry({
  queue = getUploadQueue(),
  getToken = storedToken,
  onDropped = () => {},
} = {}) {
  if (!queue || typeof window === "undefined") {
    return () => {};
  }
  const handleOnline = () => {
    queue
      .drain({ token: readToken(getToken) })
      .then((result) => surfaceDroppedUploads(result, onDropped))
      .catch(() => {});
  };
  window.addEventListener("online", handleOnline);
  // Drain at boot too, in case the app was relaunched after going offline.
  queue
    .drain({ token: readToken(getToken) })
    .then((result) => surfaceDroppedUploads(result, onDropped))
    .catch(() => {});
  return () => window.removeEventListener("online", handleOnline);
}
