// Resilient auth-token storage adapter.
//
// Browser localStorage can throw on WRITE and REMOVE (quota exceeded, Safari
// private mode, SecurityError under some embedding/policy setups), not just on
// read. Persisting an auth token after a successful login must never escape into
// the app's top-level error boundary and sign the user out of a session that is
// otherwise valid in memory.
//
// This adapter wraps a backing store (localStorage by default) with symmetric
// guarded get/set/remove and an in-memory fallback. If the backing store is
// unavailable or throws, the value lives in memory for the tab's lifetime and
// the adapter reports `isDegraded()` so the UI can warn that persistence is
// degraded — without discarding the live session.

function createAuthStorage(backing = globalThis.localStorage) {
  const fallback = new Map();
  let degraded = false;

  return {
    getItem(key) {
      if (backing) {
        try {
          const value = backing.getItem(key);
          if (value !== null && value !== undefined) {
            return value;
          }
        } catch {
          // Fall back to the in-memory copy below.
        }
      }
      return fallback.has(key) ? fallback.get(key) : "";
    },

    setItem(key, value) {
      if (backing) {
        try {
          backing.setItem(key, value);
          // Backing store is authoritative on success; drop any stale fallback.
          fallback.delete(key);
          return;
        } catch {
          // Fall through to the in-memory fallback below.
        }
      }
      fallback.set(key, value);
      degraded = true;
    },

    removeItem(key) {
      // Always drop the in-memory copy first, so a caller asking to forget a
      // token (logout) can never have it resurrected by a later getItem, even
      // if the backing store rejects the removal.
      fallback.delete(key);
      if (backing) {
        try {
          backing.removeItem(key);
        } catch {
          degraded = true;
        }
      }
    },

    isDegraded() {
      return degraded;
    },
  };
}

export { createAuthStorage };
