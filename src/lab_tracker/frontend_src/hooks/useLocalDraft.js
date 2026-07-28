import * as React from "react";

const { useCallback, useEffect, useRef, useState } = React;

const DRAFT_KEY_PREFIX = "lab-tracker:draft:";

function storageKey(key) {
  return `${DRAFT_KEY_PREFIX}${key}`;
}

function readDraft(key) {
  try {
    const raw = globalThis.localStorage?.getItem(storageKey(key));
    if (!raw) {
      return null;
    }
    const parsed = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object" || typeof parsed.value !== "string") {
      return null;
    }
    return parsed;
  } catch {
    // Unavailable storage or a corrupt entry is never worth failing over: the
    // person still has whatever the server sent.
    return null;
  }
}

function writeDraft(key, value) {
  try {
    const storage = globalThis.localStorage;
    if (!storage) {
      return;
    }
    storage.setItem(storageKey(key), JSON.stringify({ savedAt: Date.now(), value }));
  } catch {
    // localStorage may be unavailable (private mode, quota); fail silently.
  }
}

function removeDraft(key) {
  try {
    globalThis.localStorage?.removeItem(storageKey(key));
  } catch {
    // Same rationale as writeDraft.
  }
}

/**
 * Drop every stored draft. Called on sign-out: localStorage is per-origin, so on
 * a shared lab machine the next person to sign in would otherwise be offered the
 * previous person's unsent research text.
 */
function clearAllLocalDrafts() {
  try {
    const storage = globalThis.localStorage;
    if (!storage) {
      return;
    }
    const keys = [];
    for (let index = 0; index < storage.length; index += 1) {
      const name = storage.key(index);
      if (name && name.startsWith(DRAFT_KEY_PREFIX)) {
        keys.push(name);
      }
    }
    for (const name of keys) {
      storage.removeItem(name);
    }
  } catch {
    // Same rationale as writeDraft.
  }
}

/**
 * Keep a local copy of in-progress long-form text so a closed tab, a crash, or a
 * phone backgrounding the browser cannot destroy it.
 *
 * Deliberately local-only. A server-side draft would create a second, unreviewed
 * class of content sitting outside the staged/committed model and blur the
 * human-gated commit boundary, so nothing here ever reaches the API on its own.
 *
 * A recovered draft is *offered*, never applied automatically: the stored text
 * may be older than what the server now holds, and silently overwriting the
 * field would be its own kind of data loss. For the same reason the caller
 * applies the restored value itself — this hook never submits anything, which
 * also keeps a half-edited JSON payload from being auto-sent.
 *
 * @param {object} options
 * @param {string} options.key    Stable identity for this field. Falsy disables the hook.
 * @param {string} options.value  Current editor text.
 * @param {string} options.baseline Text as last saved; a value equal to it is not a draft.
 * @param {boolean} [options.enabled] Set false to suspend persistence entirely.
 */
function useLocalDraft({ key, value, baseline = "", enabled = true }) {
  const [recovered, setRecovered] = useState(null);
  // Only offer recovery for text that predates this mount. Anything typed since
  // is already on screen, and re-offering it would be noise.
  const checkedKeyRef = useRef(null);
  // Last (key, value, baseline) the write effect acted on, so it can tell a real
  // edit from a first pass over a key.
  const seenRef = useRef({ baseline: null, key: null, value: null });
  const active = Boolean(enabled && key);

  useEffect(() => {
    if (!active) {
      setRecovered(null);
      checkedKeyRef.current = null;
      return;
    }
    if (checkedKeyRef.current === key) {
      return;
    }
    checkedKeyRef.current = key;
    const stored = readDraft(key);
    if (!stored || stored.value === baseline || stored.value === value) {
      setRecovered(null);
      return;
    }
    setRecovered(stored);
  }, [active, baseline, key, value]);

  useEffect(() => {
    if (!active || checkedKeyRef.current !== key) {
      return;
    }
    // The first pass for a key only records what was on screen; it must not
    // touch storage. Writing here would do one of two harmful things:
    //   - on mount the field still holds `baseline`, so it would delete the very
    //     draft the recovery effect just offered, leaving the offer alive only
    //     in memory until the next reload; or
    //   - after the key changes (switching projects) it would stamp the previous
    //     scope's text under the new scope's key.
    const seen = seenRef.current;
    if (seen.key !== key) {
      seenRef.current = { baseline, key, value };
      return;
    }
    if (seen.value === value && seen.baseline === baseline) {
      return;
    }
    seenRef.current = { baseline, key, value };
    // Written synchronously rather than debounced: these strings are small, and
    // an abrupt close is exactly the case this exists to survive.
    if (value === baseline) {
      removeDraft(key);
      return;
    }
    writeDraft(key, value);
  }, [active, baseline, key, value]);

  const restore = useCallback(() => {
    const restoredValue = recovered?.value ?? null;
    setRecovered(null);
    return restoredValue;
  }, [recovered]);

  const discard = useCallback(() => {
    if (key) {
      // Discard throws away the *recovered* text, not whatever is on screen. If
      // the person has already typed something, keep protecting that instead of
      // leaving them with no safety net at all.
      if (value !== baseline) {
        writeDraft(key, value);
        seenRef.current = { baseline, key, value };
      } else {
        removeDraft(key);
      }
    }
    setRecovered(null);
  }, [baseline, key, value]);

  const clear = useCallback(() => {
    if (key) {
      removeDraft(key);
    }
    setRecovered(null);
  }, [key]);

  return {
    clear,
    discard,
    recoveredAt: recovered?.savedAt ?? null,
    recoveredValue: recovered?.value ?? null,
    restore,
  };
}

export { clearAllLocalDrafts, DRAFT_KEY_PREFIX, useLocalDraft };
