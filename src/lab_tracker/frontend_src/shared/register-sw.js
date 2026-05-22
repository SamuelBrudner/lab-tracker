/* Service worker registration + offline-aware queue wiring.
 *
 * Safe to call on every app boot: if the browser does not support service
 * workers (older browsers, or jsdom in tests), the helpers no-op silently.
 */

import { createUploadQueue } from "./upload-queue.js";

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

export function registerServiceWorker(scriptUrl = "/app/sw.js") {
  if (!hasServiceWorker()) {
    return Promise.resolve(null);
  }
  return navigator.serviceWorker.register(scriptUrl).catch(() => null);
}

export function installOfflineRetry({ queue = getUploadQueue() } = {}) {
  if (!queue || typeof window === "undefined") {
    return () => {};
  }
  const handleOnline = () => {
    queue.drain().catch(() => {});
  };
  window.addEventListener("online", handleOnline);
  // Drain at boot too, in case the app was relaunched after going offline.
  queue.drain().catch(() => {});
  return () => window.removeEventListener("online", handleOnline);
}
