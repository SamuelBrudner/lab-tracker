import { createHash } from "node:crypto";
import { cp, mkdir, readFile, rm, writeFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

// Usage: node scripts/build-pages-demo.mjs [outDir] [--base-path=/lab-tracker/]
//
// --base-path is the URL path the site root is published under (GitHub Pages
// project sites live at /<repository>/). GitHub Pages serves 404.html at the
// original request URL, so its asset URLs must be absolute: a relative
// `static/app.js` would resolve under a nested route such as
// /lab-tracker/app/questions/<id>/ and load the 404 page instead of the app.
const DEFAULT_BASE_PATH = "/lab-tracker/";

function parseArgs(argv) {
  let outDirArg = null;
  let basePath = DEFAULT_BASE_PATH;
  for (let index = 0; index < argv.length; index += 1) {
    const arg = argv[index];
    if (arg === "--base-path") {
      if (index + 1 >= argv.length) {
        throw new Error("--base-path requires a value such as /lab-tracker/.");
      }
      basePath = argv[index + 1];
      index += 1;
    } else if (arg.startsWith("--base-path=")) {
      basePath = arg.slice("--base-path=".length);
    } else if (arg.startsWith("--")) {
      throw new Error(`Unknown option: ${arg}`);
    } else if (outDirArg === null) {
      outDirArg = arg;
    } else {
      throw new Error(`Unexpected argument: ${arg}`);
    }
  }
  if (!/^\/(?:[A-Za-z0-9._~-]+\/)*$/.test(basePath)) {
    throw new Error(
      `--base-path must be an absolute URL path that starts and ends with "/" ` +
        `(for example /lab-tracker/ or /); got ${JSON.stringify(basePath)}.`
    );
  }
  return { outDirArg, basePath };
}

let parsedArgs;
try {
  parsedArgs = parseArgs(process.argv.slice(2));
} catch (error) {
  console.error(error.message);
  process.exit(2);
}
const { outDirArg, basePath } = parsedArgs;

const repoRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const outDir = resolve(repoRoot, outDirArg || "dist/pages-demo");
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
function renderAppHtml(staticPrefix) {
  return `<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <meta name="theme-color" content="#0d8b6f" />
    <title>Lab Tracker Demo</title>
    <link rel="manifest" href="${staticPrefix}manifest.json" />
    <link rel="apple-touch-icon" href="${staticPrefix}icon-180.png" />
    <link rel="icon" type="image/png" sizes="192x192" href="${staticPrefix}icon-192.png" />
    <link rel="stylesheet" href="${staticPrefix}styles.css?v=${assetVersion}" />
    <link rel="stylesheet" href="${staticPrefix}app.css" />
    <script>window.__LAB_TRACKER_STATIC_DEMO__ = true;</script>
    <script src="${staticPrefix}app.js?v=${assetVersion}" defer></script>
  </head>
  <body>
    <div id="app-root"></div>
    <noscript>This app requires JavaScript.</noscript>
  </body>
</html>
`;
}

// app/index.html is always served from /app/, so relative URLs work there and
// keep a locally served build usable at any prefix. 404.html is served at
// whatever path was requested, so it needs base-path-absolute URLs.
await writeFile(resolve(appDir, "index.html"), renderAppHtml("static/"), "utf-8");
await writeFile(
  resolve(outDir, "404.html"),
  renderAppHtml(`${basePath}app/static/`),
  "utf-8"
);
await writeFile(
  resolve(outDir, "index.html"),
  '<!doctype html><meta charset="utf-8"><meta http-equiv="refresh" content="0; url=app/"><link rel="canonical" href="app/"><a href="app/">Open Lab Tracker demo</a>\n',
  "utf-8"
);
await writeFile(resolve(outDir, ".nojekyll"), "", "utf-8");
