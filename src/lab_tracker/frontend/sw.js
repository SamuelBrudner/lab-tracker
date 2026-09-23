/* Lab Tracker service worker.
 *
 * Caches the app shell so the PWA loads when the network is unavailable.
 * CACHE_VERSION and the app.js/styles.css/app.css ?v tokens are stamped by
 * `npm run build:frontend` from the bytes of every shell asset listed below
 * (bundle, stylesheets, manifest and icons), so changing any of them rolls
 * the cache.
 *
 * The upload-retry queue lives in page JS (see shared/upload-queue.js); the
 * service worker intentionally does not intercept POSTs to
 * /notes/quick-capture so that submission flow and offline-queue UI stay in
 * one place.
 */

const CACHE_VERSION = "v-982e4b15d6bf";
const CACHE_NAME = `lab-tracker-shell-${CACHE_VERSION}`;
const SHELL_ASSETS = [
  "/app/",
  "/app/static/app.js?v=982e4b15d6bf",
  "/app/static/styles.css?v=982e4b15d6bf",
  "/app/static/app.css?v=982e4b15d6bf",
  "/app/static/manifest.json",
  "/app/static/icon-180.png",
  "/app/static/icon-192.png",
  "/app/static/icon-512.png",
];

const SHARE_TARGET_PATH = "/app/share-target";
const SHARE_INBOX_DB = "lab-tracker-share-inbox";
const SHARE_INBOX_STORE = "pending";
// The worker cannot reliably tell a share-sheet launch from a form another
// site submits with its referrer suppressed, so the parked inbox is bounded:
// a hostile page must not be able to fill the origin's storage quota by
// re-submitting. Keep these in step with shared/share-target-inbox.js.
const SHARE_INBOX_MAX_PENDING = 20;
// One share may be as large as the server's default upload limit (100 MiB).
const SHARE_INBOX_MAX_SHARE_BYTES = 100 * 1024 * 1024;
const SHARE_INBOX_MAX_BYTES = 200 * 1024 * 1024;
const SHARE_INBOX_MAX_AGE_MS = 7 * 24 * 60 * 60 * 1000;
const SHARE_INBOX_UPDATED_MESSAGE = "SHARE_INBOX_UPDATED";
const UPDATE_PROMPT_HANDSHAKE_MS = 1500;

let updatePromptSupported = false;

self.addEventListener("install", (event) => {
  event.waitUntil(
    Promise.all([
      caches.open(CACHE_NAME).then((cache) => cache.addAll(SHELL_ASSETS)),
      new Promise((resolve) => setTimeout(resolve, UPDATE_PROMPT_HANDSHAKE_MS)),
    ]).then(() => {
      // Legacy pages do not know how to activate a waiting worker. Preserve a
      // seamless first rollout (and quiet first install) unless a controlled,
      // prompt-capable page explicitly advertises support.
      if (!updatePromptSupported) {
        return self.skipWaiting();
      }
      return undefined;
    })
  );
});

self.addEventListener("message", (event) => {
  if (event.data?.type === "UPDATE_PROMPT_SUPPORTED") {
    updatePromptSupported = true;
    return;
  }
  if (event.data?.type === "SKIP_WAITING") {
    event.waitUntil(self.skipWaiting());
  }
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
  // parked in a small, bounded IndexedDB inbox and the capture page lists it
  // for the user to review; nothing is imported until they confirm (see
  // shared/share-target-inbox.js). Any web page can also submit a form here;
  // the explicit review step is what keeps such a submission out of the
  // user's projects.
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

// For a navigation FetchEvent the only initiator signal a service worker can
// see is request.referrer: browsers add the Origin and Sec-Fetch-* headers
// after service-worker dispatch, so they never reach this handler. A
// share-sheet launch has no referrer; a form submitted by another web page
// carries that page's URL unless it suppresses it (e.g. a no-referrer
// policy). Rejecting a foreign referrer is therefore only defence in depth;
// the control is the capture page's explicit review-and-import step, and
// the inbox bounds below cap what an unreviewed submission can park.
function crossSiteShareSignal(request) {
  const referrer = String(request.referrer || "");
  if (referrer && !referrer.startsWith("about:")) {
    let referrerOrigin = "";
    try {
      referrerOrigin = new URL(referrer).origin;
    } catch {
      return `referrer=${referrer}`;
    }
    if (referrerOrigin !== self.location.origin) {
      return `referrer=${referrerOrigin}`;
    }
  }
  return "";
}

const shareTextEncoder = new TextEncoder();

function shareRecordBytes(record) {
  let bytes = 0;
  if (record.file) {
    bytes = record.file.size;
    if (!Number.isFinite(bytes)) {
      // Unmeasurable records would make the byte cap fail open.
      throw new TypeError("Parked share file has no measurable size.");
    }
  }
  for (const value of [record.filename, record.contentType, record.title, record.text, record.url]) {
    if (value) {
      bytes += shareTextEncoder.encode(value).length;
    }
  }
  return bytes;
}

function shareExpired(record, now) {
  const receivedAt = Number(record.receivedAt);
  return !Number.isFinite(receivedAt) || now - receivedAt >= SHARE_INBOX_MAX_AGE_MS;
}

function shareRecordsFromForm(formData, receivedAt) {
  const files = formData.getAll("file");
  const title = String(formData.get("title") || "").trim();
  const text = String(formData.get("text") || "").trim();
  const url = String(formData.get("url") || "").trim();
  const records = [];
  for (const file of files) {
    if (file && (file instanceof File || file instanceof Blob)) {
      records.push({
        file,
        filename: file.name || "shared",
        contentType: file.type || "application/octet-stream",
        title,
        text,
        url,
        receivedAt,
      });
    }
  }
  if (records.length === 0 && (title || text || url)) {
    records.push({ title, text, url, receivedAt });
  }
  return records;
}

async function notifyShareInboxChanged() {
  const windows = await self.clients.matchAll({ includeUncontrolled: true, type: "window" });
  for (const client of windows) {
    client.postMessage({ type: SHARE_INBOX_UPDATED_MESSAGE });
  }
}

async function handleShareTarget(request) {
  const crossSiteSignal = crossSiteShareSignal(request);
  if (crossSiteSignal) {
    console.warn("share-target POST from another site rejected", crossSiteSignal);
    return Response.redirect("/app/capture?from-share=rejected", 303);
  }
  let redirectStatus = "empty";
  let expiredCount = 0;
  try {
    const receivedAt = Date.now();
    const records = shareRecordsFromForm(await request.formData(), receivedAt);
    if (records.length > 0) {
      const parked = await parkIncomingShares(records, receivedAt);
      redirectStatus = parked.outcome;
      expiredCount = parked.expired;
    }
  } catch (error) {
    // Stashing failed; navigate into the app with an explicit error marker so
    // the capture page can tell the user the OS share was not saved.
    console.warn("share-target inbox write failed", error);
    redirectStatus = "error";
  }
  if (redirectStatus === "1") {
    // The share is already parked: a capture page that misses this message
    // still lists it when it next mounts or becomes visible.
    try {
      await notifyShareInboxChanged();
    } catch (error) {
      console.warn("share-target inbox change notification failed", error);
    }
  } else if (redirectStatus === "full" || redirectStatus === "too-large") {
    console.warn("share-target POST refused: share inbox limit", redirectStatus);
  }
  const location = `/app/capture?from-share=${redirectStatus}`;
  // Expired shares are removed unreviewed; the capture page tells the user.
  return Response.redirect(
    expiredCount > 0 ? `${location}&share-expired=${expiredCount}` : location,
    303
  );
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

// Parks `records` (one share) in a single transaction, first dropping shares
// past SHARE_INBOX_MAX_AGE_MS. Resolves { outcome, expired }: outcome is "1"
// when parked, "too-large" when the share alone exceeds a per-share cap, or
// "full" when it does not fit beside the unexpired shares still awaiting
// review, which are never evicted for it; expired counts the dropped shares.
async function parkIncomingShares(records, now) {
  const incomingBytes = records.reduce((total, record) => total + shareRecordBytes(record), 0);
  if (records.length > SHARE_INBOX_MAX_PENDING || incomingBytes > SHARE_INBOX_MAX_SHARE_BYTES) {
    return { outcome: "too-large", expired: 0 };
  }
  const db = await openShareInbox();
  try {
    return await new Promise((resolve, reject) => {
      const tx = db.transaction(SHARE_INBOX_STORE, "readwrite");
      const store = tx.objectStore(SHARE_INBOX_STORE);
      let outcome = "";
      let expired = 0;
      let failure = null;
      const listing = store.getAll();
      listing.onsuccess = () => {
        try {
          let pendingCount = 0;
          let pendingBytes = 0;
          for (const parked of listing.result) {
            if (shareExpired(parked, now)) {
              store.delete(parked.id);
              expired += 1;
            } else {
              pendingCount += 1;
              pendingBytes += shareRecordBytes(parked);
            }
          }
          if (
            pendingCount + records.length > SHARE_INBOX_MAX_PENDING ||
            pendingBytes + incomingBytes > SHARE_INBOX_MAX_BYTES
          ) {
            outcome = "full";
            return;
          }
          for (const record of records) {
            store.add(record);
          }
          outcome = "1";
        } catch (error) {
          failure = error;
          tx.abort();
        }
      };
      tx.oncomplete = () =>
        outcome
          ? resolve({ outcome, expired })
          : reject(new Error("Share inbox listing did not complete."));
      tx.onerror = () => reject(failure || tx.error);
      tx.onabort = () =>
        reject(failure || tx.error || new Error("IndexedDB transaction aborted."));
    });
  } finally {
    db.close();
  }
}
