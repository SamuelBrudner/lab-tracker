import { describe, expect, it } from "vitest";

import { matchingClientSetup, normalizeSourceRevision } from "./client-setup.js";

const REVISION = "0123456789abcdef0123456789abcdef01234567";

describe("matchingClientSetup", () => {
  it("builds every install and verification command from one immutable revision", () => {
    const setup = matchingClientSetup(REVISION);

    expect(setup).not.toBeNull();
    expect(setup.revision).toBe(REVISION);
    expect(setup.toolInstallCommand).toContain(`@${REVISION}`);
    expect(setup.projectInstallCommand).toContain(`@${REVISION}`);
    expect(setup.verifyClientCommand).toContain(
      `--expected-revision ${REVISION}`
    );
    expect(setup.verifyMcpCommand).toContain(
      `--expected-revision ${REVISION}`
    );
    expect(setup.projectImportCommand).toContain("import lab_tracker_client");
    expect(JSON.stringify(setup)).not.toContain("@main");
  });

  it("links the per-client docs at the immutable revision, never at main", () => {
    const setup = matchingClientSetup(REVISION);

    expect(setup.clientDocsUrl).toBe(
      "https://github.com/SamuelBrudner/lab-tracker/blob/" +
        `${REVISION}/docs/agent-setup.md#choose-your-client`
    );
    expect(setup.clientDocsUrl).not.toMatch(/\/(main|master|HEAD)\//);
  });

  it("holds one ordered client list with the exact registration commands", () => {
    const { mcpClients } = matchingClientSetup(REVISION);

    expect(mcpClients.map((client) => client.id)).toEqual([
      "claude-code",
      "claude-desktop-chat",
      "codex-desktop",
      "codex-cli",
    ]);
    const commands = Object.fromEntries(
      mcpClients.map((client) => [
        client.id,
        client.commands.map((item) => item.command),
      ])
    );
    expect(commands).toEqual({
      "claude-code": [
        "claude mcp add --transport stdio --scope user lab-tracker -- lt-mcp",
        "claude mcp list",
      ],
      "claude-desktop-chat": [],
      "codex-desktop": [],
      "codex-cli": ["codex mcp add lab-tracker -- lt-mcp", "codex mcp list"],
    });
    for (const client of mcpClients) {
      expect(client.name).toBeTruthy();
      expect(client.guidance).toBeTruthy();
      for (const item of client.commands) {
        expect(item.command).not.toContain("\n");
      }
    }
  });

  it("labels the Codex CLI prerequisite and the desktop routes that need no CLI", () => {
    const byId = Object.fromEntries(
      matchingClientSetup(REVISION).mcpClients.map((client) => [client.id, client])
    );

    expect(byId["codex-cli"].name).toMatch(/PATH/);
    expect(byId["codex-cli"].guidance).toContain("command not found: codex");
    expect(byId["codex-desktop"].name).toContain("ChatGPT desktop app");
    expect(byId["codex-desktop"].guidance).toContain("MCP servers");
    expect(byId["codex-desktop"].guidance).toContain("Restart");
    expect(byId["claude-desktop-chat"].guidance).toContain("manual registration only");
    expect(byId["claude-desktop-chat"].guidance).toContain("no token");
  });

  it("words the shared copy so it is true on the Setup and the Agents page", () => {
    const setup = matchingClientSetup(REVISION);
    const byId = Object.fromEntries(
      setup.mcpClients.map((client) => [client.id, client])
    );
    const shared = [
      setup.mcpClientsIntro,
      setup.mcpVerifyNote,
      ...setup.mcpClients.map((client) => client.guidance),
    ].join("\n");

    // The Agents page has no numbered steps, and Setup step 4 does not run
    // lt setup init, so the copy names the file init creates instead.
    expect(shared).not.toMatch(/previous step/i);
    expect(byId["claude-code"].guidance).toContain("lt setup init");
    expect(byId["claude-code"].guidance).toContain(".mcp.json");
    expect(setup.mcpClientsIntro).toContain("lt setup init");
    // It is read in a browser, so the verifier runs in the reader's terminal.
    expect(shared).not.toMatch(/this terminal/i);
    expect(setup.mcpVerifyNote).toContain("your terminal");
    // The Codex CLI is a command line tool; only the ChatGPT desktop one is an app.
    expect(shared).not.toMatch(/Codex apps/i);
    expect(setup.mcpClientsIntro).toContain("Codex CLI");
  });

  it("puts no credential in any client instruction", () => {
    const text = JSON.stringify(matchingClientSetup(REVISION).mcpClients);

    expect(text).not.toContain("lpat_");
    expect(text).not.toContain("LAB_TRACKER_MCP_API_KEY");
    expect(text).not.toContain("@main");
  });

  it.each([
    "",
    "unknown",
    "main",
    "0123456",
    `${REVISION}extra`,
  ])("fails closed for an unpinned source revision: %s", (revision) => {
    expect(normalizeSourceRevision(revision)).toBe("");
    expect(matchingClientSetup(revision)).toBeNull();
  });
});
