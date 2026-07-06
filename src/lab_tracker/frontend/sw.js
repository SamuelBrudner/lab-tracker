/* Lab Tracker service worker.
 *
 * Caches the app shell so the PWA loads when the network is unavailable.
 * CACHE_VERSION and the app.js/styles.css ?v tokens are stamped from those
 * asset bytes by `npm run build:frontend`.
 *
 * The upload-retry queue lives in page JS (see shared/upload-queue.js); the
 * service worker intentionally does not intercept POSTs to
 * /notes/quick-capture so that submission flow and offline-queue UI stay in
 * one place.
 */

const CACHE_VERSION = "v-f3c1925b52b0";
const CACHE_NAME = `lab-tracker-shell-${CACHE_VERSION}`;
const SHELL_ASSETS = [
  "/app/",
  "/app/static/app.js?v=f3c1925b52b0",
  "/app/static/styles.css?v=f3c1925b52b0",
  "/app/static/manifest.json",
  "/app/static/icon-180.png",
  "/app/static/icon-192.png",
  "/app/static/icon-512.png",
];

const SHARE_TARGET_PATH = "/app/share-target";
const SHARE_INBOX_DB = "lab-tracker-share-inbox";
const SHARE_INBOX_STORE = "pending";

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
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) {
    return;
  }

  // OS share-sheet submissions land here. We intentionally don't forward the
  // POST to the API: the OS process has no auth context. Instead the file is
  // parked in a small IndexedDB inbox and the page picks it up on next load
  // (see shared/share-target-inbox.js), enqueueing it through the same
  // offline upload queue as in-app captures.
  if (request.method === "POST" && url.pathname === SHARE_TARGET_PATH) {
    event.respondWith(handleShareTarget(request));
    return;
  }

  if (request.method !== "GET") {
    return;
  }

  // Navigations under /app/* fall back to the cached app shell when offline.
  if (request.mode === "navigate" && url.pathname.startsWith("/app")) {
    event.respondWith(
      fetch(request)
        .then((response) => {
          if (response && response.status >= 500) {
            return caches.match("/app/").then((cached) => cached || response);
          }
          return response;
        })
        .catch(() => caches.match("/app/").then((cached) => cached || Response.error()))
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

async function handleShareTarget(request) {
  let redirectStatus = "empty";
  try {
    const formData = await request.formData();
    const files = formData.getAll("file");
    const title = String(formData.get("title") || "").trim();
    const text = String(formData.get("text") || "").trim();
    const url = String(formData.get("url") || "").trim();
    const receivedAt = Date.now();
    let storedCount = 0;
    for (const file of files) {
      if (file && (file instanceof File || file instanceof Blob)) {
        await storeIncomingShare({
          file,
          filename: file.name || "shared",
          contentType: file.type || "application/octet-stream",
          title,
          text,
          url,
          receivedAt,
        });
        storedCount += 1;
      }
    }
    if (storedCount === 0 && (title || text || url)) {
      await storeIncomingShare({
        title,
        text,
        url,
        receivedAt,
      });
      storedCount += 1;
    }
    redirectStatus = storedCount > 0 ? "1" : "empty";
  } catch (error) {
    // Stashing failed; navigate into the app with an explicit error marker so
    // the capture page can tell the user the OS share was not saved.
    console.warn("share-target inbox write failed", error);
    redirectStatus = "error";
  }
  return Response.redirect(`/app/capture?from-share=${redirectStatus}`, 303);
}

function openShareInbox() {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open(SHARE_INBOX_DB, 1);
    request.onupgradeneeded = () => {
      const db = request.result;
      if (!db.objectStoreNames.contains(SHARE_INBOX_STORE)) {
        db.createObjectStore(SHARE_INBOX_STORE, { keyPath: "id", autoIncrement: true });
      }
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}

async function storeIncomingShare(record) {
  const db = await openShareInbox();
  try {
    return await new Promise((resolve, reject) => {
      const tx = db.transaction(SHARE_INBOX_STORE, "readwrite");
      const req = tx.objectStore(SHARE_INBOX_STORE).add(record);
      let insertedId = null;
      req.onsuccess = () => {
        insertedId = req.result;
      };
      req.onerror = () => reject(req.error);
      tx.oncomplete = () => resolve(insertedId);
      tx.onerror = () => reject(tx.error);
      tx.onabort = () => reject(tx.error || new Error("IndexedDB transaction aborted."));
    });
  } finally {
    db.close();
  }
}
