/* Lab Tracker service worker.
 *
 * Caches the app shell so the PWA loads when the network is unavailable.
 * Bump CACHE_VERSION when shipping changes that should invalidate old caches.
 *
 * The upload-retry queue lives in page JS (see shared/upload-queue.js); the
 * service worker intentionally does not intercept POSTs to
 * /notes/quick-capture so that submission flow and offline-queue UI stay in
 * one place.
 */

const CACHE_VERSION = "v1";
const CACHE_NAME = `lab-tracker-shell-${CACHE_VERSION}`;
const SHELL_ASSETS = [
  "/app",
  "/app/static/app.js",
  "/app/static/styles.css",
  "/app/static/manifest.json",
  "/app/static/icon-180.png",
  "/app/static/icon-192.png",
  "/app/static/icon-512.png",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(SHELL_ASSETS))
  );
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) =>
        Promise.all(
          keys
            .filter((key) => key.startsWith("lab-tracker-shell-") && key !== CACHE_NAME)
            .map((key) => caches.delete(key))
        )
      )
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") {
    return;
  }
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) {
    return;
  }

  // Navigations under /app/* fall back to the cached app shell when offline.
  if (request.mode === "navigate" && url.pathname.startsWith("/app")) {
    event.respondWith(
      fetch(request).catch(() =>
        caches.match("/app").then((cached) => cached || Response.error())
      )
    );
    return;
  }

  // Static shell assets: cache-first.
  if (url.pathname.startsWith("/app/static/")) {
    event.respondWith(
      caches.match(request).then(
        (cached) =>
          cached ||
          fetch(request).then((response) => {
            if (response && response.ok) {
              const copy = response.clone();
              caches.open(CACHE_NAME).then((cache) => cache.put(request, copy));
            }
            return response;
          })
      )
    );
  }
});
