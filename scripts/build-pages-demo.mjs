import { createHash } from "node:crypto";
import { cp, mkdir, readFile, rm, writeFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const repoRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const outDir = resolve(repoRoot, process.argv[2] || "dist/pages-demo");
const frontendDir = resolve(repoRoot, "src/lab_tracker/frontend");
const appDir = resolve(outDir, "app");
const staticDir = resolve(appDir, "static");

async function staticAssetVersion() {
  const hash = createHash("sha256");
  for (const filename of ["app.js", "styles.css"]) {
    hash.update(filename);
    hash.update("\0");
    hash.update(await readFile(resolve(frontendDir, filename)));
    hash.update("\0");
  }
  return hash.digest("hex").slice(0, 12);
}

await rm(outDir, { force: true, recursive: true });
await mkdir(staticDir, { recursive: true });

const staticFiles = [
  "app.css",
  "app.js",
  "icon-180.png",
  "icon-192.png",
  "icon-512.png",
  "styles.css",
];
await Promise.all(
  staticFiles.map((filename) =>
    cp(resolve(frontendDir, filename), resolve(staticDir, filename))
  )
);

const manifest = JSON.parse(
  await readFile(resolve(frontendDir, "manifest.json"), "utf-8")
);
manifest.start_url = "./";
manifest.scope = "./";
manifest.icons = (manifest.icons || []).map((icon) => ({
  ...icon,
  src: String(icon.src || "").replace(/^\/app\/static\//, "static/"),
}));
delete manifest.share_target;
await writeFile(
  resolve(staticDir, "manifest.json"),
  `${JSON.stringify(manifest, null, 2)}\n`,
  "utf-8"
);

const assetVersion = await staticAssetVersion();
const appHtml = `<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <meta name="theme-color" content="#0d8b6f" />
    <title>Lab Tracker Demo</title>
    <link rel="manifest" href="static/manifest.json" />
    <link rel="apple-touch-icon" href="static/icon-180.png" />
    <link rel="icon" type="image/png" sizes="192x192" href="static/icon-192.png" />
    <link rel="stylesheet" href="static/styles.css?v=${assetVersion}" />
    <link rel="stylesheet" href="static/app.css" />
    <script>window.__LAB_TRACKER_STATIC_DEMO__ = true;</script>
    <script src="static/app.js?v=${assetVersion}" defer></script>
  </head>
  <body>
    <div id="app-root"></div>
    <noscript>This app requires JavaScript.</noscript>
  </body>
</html>
`;

await writeFile(resolve(appDir, "index.html"), appHtml, "utf-8");
await writeFile(resolve(outDir, "404.html"), appHtml, "utf-8");
await writeFile(
  resolve(outDir, "index.html"),
  '<!doctype html><meta charset="utf-8"><meta http-equiv="refresh" content="0; url=app/"><link rel="canonical" href="app/"><a href="app/">Open Lab Tracker demo</a>\n',
  "utf-8"
);
await writeFile(resolve(outDir, ".nojekyll"), "", "utf-8");
