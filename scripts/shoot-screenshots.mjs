// Regenerate the committed screenshots used in the README.
//
// These are dark-mode, retina (deviceScaleFactor 2) captures shot against the
// offline demo dataset (`?demo=1`, no DB or login needed) so they are fully
// reproducible.
//
// Usage:
//   node scripts/shoot-screenshots.mjs                 # spawns its own server
//   node scripts/shoot-screenshots.mjs questions full  # a subset of views
//   node scripts/shoot-screenshots.mjs narrative       # narrative review only
//   SHOT_BASE_URL=http://127.0.0.1:8000 node scripts/shoot-screenshots.mjs
//                                                      # reuse a running server
//
// Requires the Playwright chromium browser:
//   npx playwright install chromium
//
// Scope: the deterministic graph views (evidence / questions / full) and the
// seeded narrative review. Capture-composer screenshots still depend on live
// state and are shot by hand.

import { readFile, mkdir } from "node:fs/promises";
import { createServer } from "node:http";
import { basename, dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { chromium } from "playwright";

const repoRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const outDir = resolve(repoRoot, "docs/screenshots");
const frontendDir = resolve(repoRoot, "src/lab_tracker/frontend");

const GRAPH_VIEWS = new Set(["evidence", "questions", "full"]);
const ALL_VIEWS = [...GRAPH_VIEWS, "narrative"];
const requested = process.argv.slice(2);
const views = requested.length ? requested : ALL_VIEWS;
const unknown = views.filter((view) => !ALL_VIEWS.includes(view));
if (unknown.length) {
  console.error(`Unknown view(s): ${unknown.join(", ")}. Valid: ${ALL_VIEWS.join(", ")}`);
  process.exit(1);
}

// Retina + the app's own max content width (.app-shell caps at 1160px), so the
// captured card matches the existing hand-shot screenshots (~2244px wide).
const DEVICE_SCALE_FACTOR = 2;
const VIEWPORT = { width: 1200, height: 1300 };
const CARD_SELECTOR = ".project-graph-card";
const NODE_SELECTOR = ".react-flow__node";
const NARRATIVE_DRAFT_ID = "8f21b9d0-e6d2-4fd0-96c8-c0436da59e31";
// Let the React Flow fitView transform settle before capturing.
const SETTLE_MS = 1400;

const port = Number(process.env.SHOT_PORT || 8137);
const providedBase = process.env.SHOT_BASE_URL;
const baseUrl = (providedBase || `http://127.0.0.1:${port}`).replace(/\/$/, "");

async function waitForServer(url, { timeoutMs = 30000, intervalMs = 300 } = {}) {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    try {
      const res = await fetch(url, { redirect: "manual" });
      if (res.status > 0) return;
    } catch {
      // not up yet
    }
    if (Date.now() > deadline) {
      throw new Error(`Server at ${url} did not become ready within ${timeoutMs}ms`);
    }
    await new Promise((r) => setTimeout(r, intervalMs));
  }
}

async function startServer() {
  const contentTypes = {
    ".css": "text/css; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".png": "image/png",
  };
  const server = createServer(async (request, response) => {
    try {
      const pathname = new URL(request.url || "/", baseUrl).pathname;
      let assetPath = resolve(frontendDir, "index.html");
      if (pathname === "/app/sw.js") {
        assetPath = resolve(frontendDir, "sw.js");
      } else if (pathname.startsWith("/app/static/")) {
        const filename = decodeURIComponent(pathname.slice("/app/static/".length));
        if (!filename || filename !== basename(filename)) {
          response.writeHead(404);
          response.end("Not found");
          return;
        }
        assetPath = resolve(frontendDir, filename);
      } else if (pathname !== "/" && pathname !== "/app" && !pathname.startsWith("/app/")) {
        response.writeHead(404);
        response.end("Not found");
        return;
      }
      const extension = assetPath.slice(assetPath.lastIndexOf("."));
      const body = await readFile(assetPath);
      response.writeHead(200, {
        "content-type": contentTypes[extension] || "application/octet-stream",
      });
      response.end(body);
    } catch (error) {
      response.writeHead(error?.code === "ENOENT" ? 404 : 500);
      response.end(error?.code === "ENOENT" ? "Not found" : "Screenshot server error");
    }
  });
  await new Promise((resolveListen, rejectListen) => {
    server.once("error", rejectListen);
    server.listen(port, "127.0.0.1", resolveListen);
  });
  return server;
}

async function shoot() {
  await mkdir(outDir, { recursive: true });
  const browser = await chromium.launch();
  try {
    const context = await browser.newContext({
      colorScheme: "dark",
      deviceScaleFactor: DEVICE_SCALE_FACTOR,
      viewport: VIEWPORT,
    });
    const page = await context.newPage();
    for (const view of views) {
      if (GRAPH_VIEWS.has(view)) {
        await page.setViewportSize(VIEWPORT);
        const url = `${baseUrl}/app/graph?demo=1&view=${view}`;
        await page.goto(url, { waitUntil: "networkidle" });
        await page.waitForSelector(NODE_SELECTOR, { timeout: 15000 });
        await page.evaluate(() => document.fonts.ready);
        await page.waitForTimeout(SETTLE_MS);
        const card = page.locator(CARD_SELECTOR);
        const outPath = resolve(outDir, `project-graph-${view}.png`);
        await card.screenshot({ path: outPath });
        console.log(`✓ ${view} -> ${outPath}`);
        continue;
      }

      const url = `${baseUrl}/app/batches/${NARRATIVE_DRAFT_ID}?demo=1`;
      await page.setViewportSize({ width: 1200, height: 1800 });
      await page.goto(url, { waitUntil: "networkidle" });
      await page.addStyleTag({
        content: `
          .audio-review-console,
          .detail-actions,
          .review-actions,
          .review-meta,
          .review-unsure {
            display: none !important;
          }
        `,
      });
      await page.getByRole("button", { name: "Narrative" }).click();
      const citation = page.getByRole("button", {
        name: /Proposed edit 1: Proposed new question/,
      });
      await citation.hover();
      const citationCard = page.getByRole("group", { name: "Proposed edit 1 details" });
      await citationCard.waitFor();
      // The hosted demo remains read-only. The screenshot shows the normal
      // contributor controls without making a request or changing that safety
      // boundary.
      await citationCard.locator(":disabled").evaluateAll((elements) => {
        elements.forEach((element) => {
          element.disabled = false;
        });
      });
      await page.evaluate(() => document.fonts.ready);

      const report = page.locator(".daily-review-report");
      const narrative = page.locator(".review-narrative");
      const [reportBox, narrativeBox, citationBox] = await Promise.all([
        report.boundingBox(),
        narrative.boundingBox(),
        citationCard.boundingBox(),
      ]);
      if (!reportBox || !narrativeBox || !citationBox) {
        throw new Error("Narrative review did not produce a stable screenshot layout.");
      }
      const outPath = resolve(outDir, "daily-review-narrative.png");
      await page.screenshot({
        clip: {
          height:
            Math.max(narrativeBox.y + narrativeBox.height, citationBox.y + citationBox.height) -
            reportBox.y +
            28,
          width: reportBox.width,
          x: reportBox.x,
          y: reportBox.y,
        },
        path: outPath,
      });
      console.log(`✓ ${view} -> ${outPath}`);
    }
  } finally {
    await browser.close();
  }
}

let server = null;
try {
  if (providedBase) {
    console.log(`Using existing server at ${baseUrl}`);
    await waitForServer(`${baseUrl}/app`);
  } else {
    console.log(`Starting server on port ${port}...`);
    server = await startServer();
  }
  await shoot();
  console.log("Done.");
} finally {
  if (server) {
    await new Promise((resolveClose, rejectClose) => {
      server.close((error) => (error ? rejectClose(error) : resolveClose()));
    });
  }
}
