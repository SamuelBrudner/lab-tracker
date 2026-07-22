import { describe, expect, it } from "vitest";

import { createAuthStorage } from "./auth-storage.js";

function mapBackedStore() {
  const backing = new Map();
  return {
    backing,
    store: {
      getItem: (key) => (backing.has(key) ? backing.get(key) : null),
      setItem: (key, value) => backing.set(key, value),
      removeItem: (key) => backing.delete(key),
    },
  };
}

function throwingStore() {
  return {
    getItem: () => {
      throw new Error("SecurityError");
    },
    setItem: () => {
      throw new Error("QuotaExceededError");
    },
    removeItem: () => {
      throw new Error("SecurityError");
    },
  };
}

describe("createAuthStorage", () => {
  it("reads/writes through the backing store on the happy path and is not degraded", () => {
    const { backing, store } = mapBackedStore();
    const storage = createAuthStorage(store);

    storage.setItem("tok", "abc");
    expect(backing.get("tok")).toBe("abc");
    expect(storage.getItem("tok")).toBe("abc");
    expect(storage.isDegraded()).toBe(false);

    storage.removeItem("tok");
    expect(backing.has("tok")).toBe(false);
    expect(storage.getItem("tok")).toBe("");
  });

  it("falls back to memory and marks degraded when a write throws", () => {
    const storage = createAuthStorage(throwingStore());

    storage.setItem("tok", "abc"); // backing throws -> in-memory
    expect(storage.isDegraded()).toBe(true);
    expect(storage.getItem("tok")).toBe("abc"); // still readable in-memory
  });

  it("keeps a failed write authoritative over a stale readable backing value", () => {
    const backing = new Map([["tok", "old"]]);
    const storage = createAuthStorage({
      getItem: (key) => backing.get(key) ?? null,
      setItem: () => {
        throw new Error("QuotaExceededError");
      },
      removeItem: (key) => backing.delete(key),
    });

    expect(storage.getItem("tok")).toBe("old");
    storage.setItem("tok", "new");
    expect(storage.getItem("tok")).toBe("new");
    expect(storage.isDegraded()).toBe(true);
  });

  it("uses its successful-write shadow when a later backing read fails", () => {
    let readsThrow = false;
    const backing = new Map();
    const storage = createAuthStorage({
      getItem: (key) => {
        if (readsThrow) {
          throw new Error("SecurityError");
        }
        return backing.get(key) ?? null;
      },
      setItem: (key, value) => backing.set(key, value),
      removeItem: (key) => backing.delete(key),
    });

    storage.setItem("tok", "new");
    readsThrow = true;
    expect(storage.getItem("tok")).toBe("new");
    expect(storage.isDegraded()).toBe(true);
  });

  it("drops the in-memory copy on removeItem even when the backing throws (logout)", () => {
    const storage = createAuthStorage(throwingStore());
    storage.setItem("tok", "abc");
    storage.removeItem("tok"); // backing.removeItem throws, memory still cleared
    expect(storage.getItem("tok")).toBe(""); // never resurrected
    expect(storage.isDegraded()).toBe(true);
  });

  it("persists an empty logout value when remove throws but set still works", () => {
    const backing = new Map([["tok", "old"]]);
    const storage = createAuthStorage({
      getItem: (key) => backing.get(key) ?? null,
      setItem: (key, value) => backing.set(key, value),
      removeItem: () => {
        throw new Error("SecurityError");
      },
    });

    storage.removeItem("tok");
    expect(backing.get("tok")).toBe("");
    expect(storage.getItem("tok")).toBe("");
    expect(storage.isDegraded()).toBe(true);
  });

  it("keeps a logout tombstone authoritative when remove and empty-write both fail", () => {
    const storage = createAuthStorage({
      getItem: () => "old",
      setItem: () => {
        throw new Error("QuotaExceededError");
      },
      removeItem: () => {
        throw new Error("SecurityError");
      },
    });

    storage.removeItem("tok");
    expect(storage.getItem("tok")).toBe("");
    expect(storage.isDegraded()).toBe(true);
  });

  it("works with no backing store at all, using the in-memory fallback", () => {
    const storage = createAuthStorage(null);
    expect(storage.isDegraded()).toBe(true);
    expect(storage.getItem("tok")).toBe("");
    storage.setItem("tok", "abc");
    expect(storage.getItem("tok")).toBe("abc");
    expect(storage.isDegraded()).toBe(true);
  });

  it("guards access to the default localStorage getter itself", () => {
    const descriptor = Object.getOwnPropertyDescriptor(globalThis, "localStorage");
    Object.defineProperty(globalThis, "localStorage", {
      configurable: true,
      get() {
        throw new Error("SecurityError");
      },
    });
    try {
      const storage = createAuthStorage();
      expect(storage.isDegraded()).toBe(true);
      storage.setItem("tok", "in-memory");
      expect(storage.getItem("tok")).toBe("in-memory");
    } finally {
      Object.defineProperty(globalThis, "localStorage", descriptor);
    }
  });
});
