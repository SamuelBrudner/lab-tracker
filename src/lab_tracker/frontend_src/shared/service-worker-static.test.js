import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

const serviceWorkerSource = readFileSync(
  resolve(process.cwd(), "src/lab_tracker/frontend/sw.js"),
  "utf8"
);

describe("service worker source", () => {
  it("waits for share-inbox transaction completion before resolving writes", () => {
    expect(serviceWorkerSource).toContain("tx.oncomplete");
    expect(serviceWorkerSource).toContain("resolve(insertedId)");
    expect(serviceWorkerSource).toContain("IndexedDB transaction aborted");
  });

  it("keeps cache and asset versions aligned for the rebuilt bundle", () => {
    expect(serviceWorkerSource).toContain('CACHE_VERSION = "v18"');
    expect(serviceWorkerSource).toContain("/app/static/app.js?v=18");
    expect(serviceWorkerSource).toContain("/app/static/styles.css?v=18");
  });
});
