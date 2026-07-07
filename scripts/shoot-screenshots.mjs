// Regenerate the committed project-graph screenshots used in the README.
//
// These are dark-mode, retina (deviceScaleFactor 2) captures of the
// `.project-graph-card` element, shot against the offline demo dataset
// (`?demo=1`, no DB or login needed) so they are fully reproducible.
//
// Usage:
//   node scripts/shoot-screenshots.mjs                 # spawns its own server
//   node scripts/shoot-screenshots.mjs questions full  # a subset of views
//   SHOT_BASE_URL=http://127.0.0.1:8000 node scripts/shoot-screenshots.mjs
//                                                      # reuse a running server
//
// Requires the Playwright chromium browser:
//   npx playwright install chromium
//
// Scope: the deterministic graph views (evidence / questions / full). Other
// README screenshots depend on live capture/review state and are still shot by
// hand; add them here if that ever becomes reproducible in demo mode.

import { spawn } from "node:child_process";
import { mkdir } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { chromium } from "playwright";

const repoRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const outDir = resolve(repoRoot, "docs/screenshots");

const ALL_VIEWS = ["evidence", "questions", "full"];
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
  const child = spawn(
    "uv",
    ["run", "uvicorn", "lab_tracker.asgi:app", "--port", String(port)],
    { cwd: repoRoot, stdio: ["ignore", "inherit", "inherit"] },
  );
  child.on("exit", (code, signal) => {
    // Ignore the SIGTERM we send during our own cleanup.
    if (!child.stopping && code && code !== 0) {
      console.error(`uvicorn exited early (code ${code}, signal ${signal})`);
    }
  });
  await waitForServer(`${baseUrl}/app`);
  return child;
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
      const url = `${baseUrl}/app/graph?demo=1&view=${view}`;
      await page.goto(url, { waitUntil: "networkidle" });
      await page.waitForSelector(NODE_SELECTOR, { timeout: 15000 });
      await page.evaluate(() => document.fonts.ready);
      await page.waitForTimeout(SETTLE_MS);
      const card = page.locator(CARD_SELECTOR);
      const outPath = resolve(outDir, `project-graph-${view}.png`);
      await card.screenshot({ path: outPath });
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
    server.stopping = true;
    server.kill("SIGTERM");
  }
}
