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

function normalizeTextAssetBytes(buffer) {
  return Buffer.from(buffer.toString("utf8").replace(/\r\n/g, "\n"), "utf8");
}

// Every shell asset the service worker serves cache-first feeds the version,
// so changing any of them rolls the cache (scripts/build-frontend.mjs).
const VERSIONED_TEXT_ASSETS = ["app.js", "styles.css", "app.css", "manifest.json"];
const VERSIONED_BINARY_ASSETS = ["icon-180.png", "icon-192.png", "icon-512.png"];

function extractVersionedShellAssets(source) {
  return Object.fromEntries(
    Array.from(
      source.matchAll(
        /\/app\/static\/(?<asset>app\.js|styles\.css|app\.css)\?v=(?<version>[a-f0-9]{12})/g
      )
    ).map((match) => [match.groups.asset, match.groups.version])
  );
}

function extractCacheVersion(source) {
  const match = source.match(/const CACHE_VERSION = "(?<version>v-[a-f0-9]{12})"/);
  expect(match, "service worker CACHE_VERSION").not.toBeNull();
  return match.groups.version;
}

function expectedStaticAssetVersion() {
  const hash = createHash("sha256");
  for (const filename of [...VERSIONED_TEXT_ASSETS, ...VERSIONED_BINARY_ASSETS]) {
    const bytes = readFileSync(resolve(frontendStaticDir, filename));
    hash.update(filename);
    hash.update("\0");
    hash.update(
      VERSIONED_TEXT_ASSETS.includes(filename) ? normalizeTextAssetBytes(bytes) : bytes
    );
    hash.update("\0");
  }
  return hash.digest("hex").slice(0, 12);
}

describe("service worker source", () => {
  it("waits for share-inbox transaction completion before resolving writes", () => {
    expect(serviceWorkerSource).toContain("tx.oncomplete");
    expect(serviceWorkerSource).toContain("resolve({ outcome, expired })");
    expect(serviceWorkerSource).toContain("IndexedDB transaction aborted");
  });

  it("stores fileless share-target text and URL records and redirects with explicit status", () => {
    expect(serviceWorkerSource).toContain('formData.get("url")');
    expect(serviceWorkerSource).toContain("records.length === 0 && (title || text || url)");
    expect(serviceWorkerSource).toContain("from-share=${redirectStatus}");
    expect(serviceWorkerSource).toContain('redirectStatus = "error"');
  });

  it("caches the canonical app shell and falls back for failed navigations", () => {
    expect(serviceWorkerSource).toContain('"/app/"');
    expect(serviceWorkerSource).toContain('caches.match("/app/")');
    expect(serviceWorkerSource).toContain("response.status >= 500");
    expect(serviceWorkerSource).toContain("cached || response");
    expect(serviceWorkerSource).toContain("cached || Response.error()");
  });

  it("prompts capable clients while preserving automatic legacy rollout", () => {
    expect(serviceWorkerSource).toContain("UPDATE_PROMPT_HANDSHAKE_MS");
    expect(serviceWorkerSource).toContain(
      'event.data?.type === "UPDATE_PROMPT_SUPPORTED"'
    );
    expect(serviceWorkerSource).toContain("updatePromptSupported = true");
    expect(serviceWorkerSource).toContain("if (!updatePromptSupported)");
    expect(serviceWorkerSource).toContain('event.data?.type === "SKIP_WAITING"');
    expect(serviceWorkerSource).toContain("self.skipWaiting()");
  });

  it("keeps cache and asset versions aligned across the shell files", () => {
    const cacheVersion = extractCacheVersion(serviceWorkerSource);
    const expectedAssetVersion = expectedStaticAssetVersion();
    const indexAssetVersions = extractVersionedShellAssets(indexHtmlSource);
    const serviceWorkerAssetVersions = extractVersionedShellAssets(serviceWorkerSource);

    expect(cacheVersion).toBe(`v-${expectedAssetVersion}`);
    expect(indexAssetVersions).toEqual({
      "app.css": expectedAssetVersion,
      "app.js": expectedAssetVersion,
      "styles.css": expectedAssetVersion,
    });
    expect(serviceWorkerAssetVersions).toEqual(indexAssetVersions);
  });

  it("precaches every stylesheet the app shell loads", () => {
    const stylesheets = Array.from(
      indexHtmlSource.matchAll(/<link rel="stylesheet" href="(?<href>[^"]+)"/g)
    ).map((match) => match.groups.href);

    expect(stylesheets).toHaveLength(2);
    for (const href of stylesheets) {
      expect(serviceWorkerSource).toContain(`"${href}"`);
    }
  });
});
