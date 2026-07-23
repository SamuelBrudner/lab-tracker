// Boot a disposable Lab Tracker server for the Playwright e2e suite.
//
// Creates a throwaway SQLite database + storage dirs, runs the migrations, and
// execs uvicorn. Playwright's `webServer` config runs this and waits for /app;
// it SIGTERMs the process on teardown, at which point the temp dir is removed.
//
// Env:
//   E2E_PORT         port to bind (default 8177)
//   E2E_AUTH_ENABLED "true" to run auth-enabled (default "false")
//   E2E_BOOTSTRAP_TOKEN  first-admin bootstrap token when auth is enabled
import { spawn, spawnSync } from "node:child_process";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const repoRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const port = process.env.E2E_PORT || "8177";
const authEnabled = process.env.E2E_AUTH_ENABLED === "true";
const workDir = mkdtempSync(join(tmpdir(), "lt-e2e-"));

const serverEnv = {
  ...process.env,
  LAB_TRACKER_ENVIRONMENT: "local",
  LAB_TRACKER_AUTH_ENABLED: authEnabled ? "true" : "false",
  LAB_TRACKER_DATABASE_URL: `sqlite+pysqlite:///${join(workDir, "e2e.db")}`,
  LAB_TRACKER_FILE_STORAGE_PATH: join(workDir, "file-storage"),
  LAB_TRACKER_NOTE_STORAGE_PATH: join(workDir, "note-storage"),
  LAB_TRACKER_AUTH_SECRET_KEY: "e2e-secret-key",
};
if (authEnabled && process.env.E2E_BOOTSTRAP_TOKEN) {
  serverEnv.LAB_TRACKER_BOOTSTRAP_ADMIN_TOKEN = process.env.E2E_BOOTSTRAP_TOKEN;
}

function run(cmd, args) {
  const result = spawnSync(cmd, args, { cwd: repoRoot, env: serverEnv, stdio: "inherit" });
  if (result.status !== 0) {
    throw new Error(`${cmd} ${args.join(" ")} failed (${result.status})`);
  }
}

let server = null;
function cleanup() {
  if (server && !server.killed) {
    try {
      server.kill("SIGTERM");
    } catch {
      // already gone
    }
  }
  rmSync(workDir, { recursive: true, force: true });
}
process.on("SIGTERM", () => {
  cleanup();
  process.exit(0);
});
process.on("SIGINT", () => {
  cleanup();
  process.exit(0);
});
process.on("exit", cleanup);

run("uv", ["run", "alembic", "upgrade", "head"]);
// Seed a demo project (questions/notes/etc.) so routing/link flows have real
// rendered content to navigate. Auth-disabled runs act as the local admin.
if (!authEnabled) {
  run("uv", ["run", "lab-tracker", "seed-demo"]);
}

server = spawn(
  "uv",
  ["run", "uvicorn", "lab_tracker.asgi:app", "--host", "127.0.0.1", "--port", port],
  { cwd: repoRoot, env: serverEnv, stdio: "inherit" }
);
server.on("exit", (code) => {
  cleanup();
  process.exit(code ?? 0);
});
