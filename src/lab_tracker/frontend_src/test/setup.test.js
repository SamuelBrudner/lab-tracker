import { describe, expect, it } from "vitest";

import { installFetchMock } from "./utils.js";

// The shared afterEach in setup.js must undo every vi.stubGlobal, or a fetch
// mock installed by one test silently answers requests in the next. The second
// test observes the cleanup after the first, so pin their order even when the
// suite runs with --sequence.shuffle.
describe("shared test setup", { shuffle: false }, () => {
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
