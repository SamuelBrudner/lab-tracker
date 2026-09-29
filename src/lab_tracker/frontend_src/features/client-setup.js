const SOURCE_REPOSITORY =
  "https://github.com/SamuelBrudner/lab-tracker.git";
const SOURCE_WEB_URL = SOURCE_REPOSITORY.replace(/\.git$/, "");
const CLIENT_SETUP_DOC_PATH = "docs/agent-setup.md";
const CLIENT_SETUP_DOC_ANCHOR = "choose-your-client";
const FULL_GIT_REVISION = /^[0-9a-f]{40}$/i;

// One list drives the Setup and Agents pages, so they cannot drift apart. The
// per-client steps, prerequisites, and verification live in
// docs/agent-setup.md ("Choose your client"); keep the exact commands here in
// step with it (tests/test_docs_drift.py checks that they are documented).
const MCP_CLIENTS_INTRO =
  "Each assistant registers Lab Tracker in its own settings, so follow only " +
  "the one you use. Claude Code reads the repository .mcp.json that setup " +
  "writes. Claude Desktop chat and both Codex apps keep their registration in " +
  "user-level settings that Lab Tracker never writes.";

const MCP_CLIENTS = [
  {
    id: "claude-code",
    name: "Claude Code (terminal, IDE, or the Claude Desktop Code tab)",
    guidance:
      "The repository .mcp.json from the previous step is enough, and it " +
      "carries no token. Open Claude Code in that repository and approve the " +
      "server when prompted. Register for your user account only for access " +
      "outside that repository.",
    commands: [
      {
        title:
          "Claude Code: register for your user account (optional with repo .mcp.json)",
        command:
          "claude mcp add --transport stdio --scope user lab-tracker -- lt-mcp",
      },
      {
        title: "Claude Code: check the connection",
        command: "claude mcp list",
      },
    ],
  },
  {
    id: "claude-desktop-chat",
    name: "Claude Desktop chat",
    guidance:
      "Supported by manual registration only. lt never writes " +
      "claude_desktop_config.json. Open Settings, then Developer, then Edit " +
      "Config, add a lab-tracker entry whose command is the absolute path to " +
      "lt-mcp with no token, then fully quit and reopen the app.",
    commands: [],
  },
  {
    id: "codex-desktop",
    name: "Codex in the ChatGPT desktop app",
    guidance:
      "These steps do not use the codex command. Open Settings, then MCP " +
      "servers, then Add server. Choose STDIO, enter the absolute path to " +
      "lt-mcp as the command, save, then select Restart. Type /mcp in the " +
      "composer to see connected servers.",
    commands: [],
  },
  {
    id: "codex-cli",
    name: "Codex CLI (needs the codex command on your PATH)",
    guidance:
      "The codex command comes from the Codex CLI, which has its own install " +
      "steps in OpenAI's documentation and must be on your PATH for codex mcp " +
      "to run. A shell that reports \"command not found: codex\" cannot find " +
      "it: install the CLI or add its directory to PATH first.",
    commands: [
      {
        title: "Codex CLI: register Lab Tracker MCP",
        command: "codex mcp add lab-tracker -- lt-mcp",
      },
      {
        title: "Codex CLI: confirm registration",
        command: "codex mcp list",
      },
    ],
  },
];

const MCP_VERIFY_TITLE =
  "Any client: verify that lt-mcp launches, authenticates, and matches this server";
const MCP_VERIFY_NOTE =
  "The verifier runs in this terminal's environment. For a desktop app, add " +
  "--command with the absolute path you registered, then ask the assistant " +
  "to call lab_tracker_list_projects with limit 1: a registration listing " +
  "alone does not prove authentication.";
const MCP_DOCS_LINK_LABEL = "Full per-client steps and verification";

function normalizeSourceRevision(sourceRevision) {
  const revision = String(sourceRevision || "").trim().toLowerCase();
  return FULL_GIT_REVISION.test(revision) ? revision : "";
}

function matchingClientSetup(sourceRevision) {
  const revision = normalizeSourceRevision(sourceRevision);
  if (!revision) {
    return null;
  }

  const installRequirement =
    `lab-tracker @ git+${SOURCE_REPOSITORY}@${revision}`;
  return {
    clientDocsLabel: MCP_DOCS_LINK_LABEL,
    clientDocsUrl:
      `${SOURCE_WEB_URL}/blob/${revision}/${CLIENT_SETUP_DOC_PATH}` +
      `#${CLIENT_SETUP_DOC_ANCHOR}`,
    installRequirement,
    mcpClients: MCP_CLIENTS,
    mcpClientsIntro: MCP_CLIENTS_INTRO,
    mcpVerifyNote: MCP_VERIFY_NOTE,
    mcpVerifyTitle: MCP_VERIFY_TITLE,
    projectImportCommand:
      'uv run python -c "import lab_tracker_client; print(\'lab_tracker_client import OK\')"',
    projectInstallCommand: `uv add "${installRequirement}"`,
    revision,
    toolInstallCommand: `uv tool install --force "${installRequirement}"`,
    verifyClientCommand:
      `uv run lt setup verify-client --expected-revision ${revision}`,
    verifyMcpCommand:
      `lt setup verify-mcp --expected-revision ${revision}`,
  };
}

export { matchingClientSetup, normalizeSourceRevision };
