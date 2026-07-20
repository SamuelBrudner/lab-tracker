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

  it("drops the in-memory copy on removeItem even when the backing throws (logout)", () => {
    const storage = createAuthStorage(throwingStore());
    storage.setItem("tok", "abc");
    storage.removeItem("tok"); // backing.removeItem throws, memory still cleared
    expect(storage.getItem("tok")).toBe(""); // never resurrected
    expect(storage.isDegraded()).toBe(true);
  });

  it("works with no backing store at all, using the in-memory fallback", () => {
    const storage = createAuthStorage(null);
    expect(storage.getItem("tok")).toBe("");
    storage.setItem("tok", "abc");
    expect(storage.getItem("tok")).toBe("abc");
    expect(storage.isDegraded()).toBe(true);
  });
});
