import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

const serviceWorkerSource = readFileSync(
  resolve(process.cwd(), "src/lab_tracker/frontend/sw.js"),
  "utf8"
);
const indexHtmlSource = readFileSync(
  resolve(process.cwd(), "src/lab_tracker/frontend/index.html"),
  "utf8"
);
const frontendStaticDir = resolve(process.cwd(), "src/lab_tracker/frontend");

function extractVersionedShellAssets(source) {
  return Object.fromEntries(
    source
      .matchAll(
        /\/app\/static\/(?<asset>app\.js|styles\.css)\?v=(?<version>[a-f0-9]{12})/g
      )
      .map((match) => [match.groups.asset, match.groups.version])
  );
}

function extractCacheVersion(source) {
  const match = source.match(/const CACHE_VERSION = "(?<version>v-[a-f0-9]{12})"/);
  expect(match, "service worker CACHE_VERSION").not.toBeNull();
  return match.groups.version;
}

function expectedStaticAssetVersion() {
  const hash = createHash("sha256");
  for (const filename of ["app.js", "styles.css"]) {
    hash.update(filename);
    hash.update("\0");
    hash.update(readFileSync(resolve(frontendStaticDir, filename)));
    hash.update("\0");
  }
  return hash.digest("hex").slice(0, 12);
}

describe("service worker source", () => {
  it("waits for share-inbox transaction completion before resolving writes", () => {
    expect(serviceWorkerSource).toContain("tx.oncomplete");
    expect(serviceWorkerSource).toContain("resolve(insertedId)");
    expect(serviceWorkerSource).toContain("IndexedDB transaction aborted");
  });

  it("stores fileless share-target text and URL records and redirects with explicit status", () => {
    expect(serviceWorkerSource).toContain('formData.get("url")');
    expect(serviceWorkerSource).toContain("storedCount === 0 && (title || text || url)");
    expect(serviceWorkerSource).toContain("from-share=${redirectStatus}");
    expect(serviceWorkerSource).toContain('redirectStatus = "error"');
    expect(serviceWorkerSource).toContain('redirectStatus = storedCount > 0 ? "1" : "empty"');
  });

  it("caches the canonical app shell and falls back for failed navigations", () => {
    expect(serviceWorkerSource).toContain('"/app/"');
    expect(serviceWorkerSource).toContain('caches.match("/app/")');
    expect(serviceWorkerSource).toContain("response.status >= 500");
    expect(serviceWorkerSource).toContain("cached || response");
    expect(serviceWorkerSource).toContain("cached || Response.error()");
  });

  it("keeps cache and asset versions aligned across the shell files", () => {
    const cacheVersion = extractCacheVersion(serviceWorkerSource);
    const expectedAssetVersion = expectedStaticAssetVersion();
    const indexAssetVersions = extractVersionedShellAssets(indexHtmlSource);
    const serviceWorkerAssetVersions = extractVersionedShellAssets(serviceWorkerSource);

    expect(cacheVersion).toBe(`v-${expectedAssetVersion}`);
    expect(indexAssetVersions).toEqual({
      "app.js": expectedAssetVersion,
      "styles.css": expectedAssetVersion,
    });
    expect(serviceWorkerAssetVersions).toEqual(indexAssetVersions);
  });
});
