import { describe, expect, it } from "vitest";

import { installFetchMock } from "./utils.js";

// The shared afterEach in setup.js must undo every vi.stubGlobal, or a fetch
// mock installed by one test silently answers requests in the next.
describe("shared test setup", () => {
  const originalFetch = globalThis.fetch;
  let stubbedFetch = null;

  it("lets a test stub fetch", () => {
    stubbedFetch = installFetchMock([]);
    expect(globalThis.fetch).toBe(stubbedFetch);
  });

  it("restores the real fetch before the next test", () => {
    expect(stubbedFetch).not.toBeNull();
    expect(globalThis.fetch).not.toBe(stubbedFetch);
    expect(globalThis.fetch).toBe(originalFetch);
  });
});
