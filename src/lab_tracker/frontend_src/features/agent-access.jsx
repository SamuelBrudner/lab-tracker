import * as React from "react";

import { auth as authGateway } from "../shared/gateways/index.js";
import { formatDate } from "../shared/formatters.js";
import { matchingClientSetup } from "./client-setup.js";

const { useCallback, useEffect, useMemo, useState } = React;

const DAY_MS = 24 * 60 * 60 * 1000;
/* The server rejects expiries beyond its 90-day cap against its own clock,
   so shave a margin off the computed expiry to absorb client clock skew. */
const EXPIRY_SKEW_MARGIN_MS = 15 * 60 * 1000;
const ROLE_RANK = { admin: 2, editor: 1, viewer: 0 };

const EXPIRY_CHOICES = [
  { days: 7, label: "7 days" },
  { days: 30, label: "30 days" },
  { days: 90, label: "90 days (maximum)" },
];

/* The server caps the stored role at the signed-in user's role, so only
   offer levels the user can actually mint. */
const ACCESS_LEVELS = [
  {
    value: "stage",
    label: "Read + stage evidence (recommended for hooks)",
    description:
      "Reads context and syncs staged notes from captures and commit hooks. Never commits.",
    role: "editor",
    readOnly: false,
  },
  {
    value: "read",
    label: "Read-only",
    description: "Decision context, search, and graph reads. Cannot sync captured evidence.",
    role: "viewer",
    readOnly: true,
  },
  {
    value: "scheduler",
    label: "Scheduler trigger (admin)",
    description: "Only the daily-review run-due trigger — no reads, no other writes.",
    role: "admin",
    readOnly: true,
    scope: "batch_run_due",
  },
];

function isPrivateHost(hostname) {
  const host = String(hostname || "").toLowerCase();
  if (
    host === "localhost" ||
    host === "0.0.0.0" ||
    host === "::1" ||
    host === "[::1]" ||
    host.endsWith(".local") ||
    host.endsWith(".localhost")
  ) {
    return true;
  }
  return (
    /^127\./.test(host) ||
    /^10\./.test(host) ||
    /^192\.168\./.test(host) ||
    /^172\.(1[6-9]|2\d|3[01])\./.test(host)
  );
}

/* Prompt instead of embedding the one-time token in a copied command, where it
   would be retained in shell history. The consented `--save-token` step writes
   the permission-hardened connection profile read by both the CLI and lt-mcp;
   each block then clears its temporary process environment. */
function connectCommands(baseUrl, projectId = "") {
  const projectOption = projectId ? ` --project ${projectId}` : "";
  return {
    posix: [
      "(",
      "  trap 'stty echo < /dev/tty 2>/dev/null || :; unset LAB_TRACKER_ACCESS_TOKEN' 0",
      "  trap 'exit 130' HUP INT TERM",
      "  printf 'Lab Tracker one-time token: ' >&2",
      "  stty -echo < /dev/tty",
      "  IFS= read -r LAB_TRACKER_ACCESS_TOKEN < /dev/tty",
      "  stty echo < /dev/tty",
      "  printf '\\n' >&2",
      "  export LAB_TRACKER_ACCESS_TOKEN",
      `  lt setup connect --base-url ${baseUrl}${projectOption} --save-token --yes`,
      ")",
    ].join("\n"),
    powershell: [
      '$labTrackerSecureToken = Read-Host "Lab Tracker one-time token" -AsSecureString',
      "try {",
      "  $env:LAB_TRACKER_ACCESS_TOKEN = " +
        '([System.Net.NetworkCredential]::new("", $labTrackerSecureToken)).Password',
      `  lt setup connect --base-url ${baseUrl}${projectOption} --save-token --yes`,
      "} finally {",
      "  Remove-Item Env:LAB_TRACKER_ACCESS_TOKEN -ErrorAction SilentlyContinue",
      "  $labTrackerSecureToken.Dispose()",
      "  Remove-Variable labTrackerSecureToken -ErrorAction SilentlyContinue",
      "}",
    ].join("\n"),
  };
}

function repoCommands(project, canStageEvidence) {
  const commands = [
    {
      command: "lt setup init --install-skills --dry-run",
      title: "1. Preview repository and skill setup",
    },
    {
      command: "lt setup init --install-skills --yes",
      title: "2. Apply repository and skill setup",
    },
  ];
  if (project?.project_id) {
    commands.push(
      {
        command: `lt project bind --project-id ${project.project_id} --dry-run`,
        title: `3. Preview binding to ${project.name}`,
      },
      {
        command: `lt project bind --project-id ${project.project_id} --yes`,
        title: `4. Bind to ${project.name}`,
      }
    );
    if (canStageEvidence) {
      commands.push(
        {
          command: `lt hooks install --project ${project.project_id} --dry-run`,
          title: "5. Preview commit capture hook",
        },
        {
          command: `lt hooks install --project ${project.project_id} --yes`,
          title: "6. Install commit capture hook",
        }
      );
    }
  }
  commands.push({
    command: "lt setup status",
    title: `${commands.length + 1}. Verify repository setup`,
  });
  return commands;
}

function codexMcpCommands(clientSetup) {
  if (!clientSetup) {
    return [];
  }
  const commands = [
    {
      command: "codex mcp add lab-tracker -- lt-mcp",
      title: "1. Register Lab Tracker MCP for Codex",
    },
  ];
  commands.push({
    command: clientSetup.verifyMcpCommand,
    title: "2. Launch MCP and verify health, auth, and client revision",
  });
  commands.push({
    command: "codex mcp list",
    title: `${commands.length + 1}. Confirm Codex registration`,
  });
  return commands;
}

function CommandSnippet({ title, command, onCopy }) {
  return (
    <div className="command-snippet">
      <div className="row-between">
        <strong>{title}</strong>
        <button type="button" className="btn-secondary" onClick={onCopy}>
          Copy
        </button>
      </div>
      <pre className="command-block">
        <code>{command}</code>
      </pre>
    </div>
  );
}

function AgentAccessPage({
  token,
  user,
  authEnabled,
  selectedProject = null,
  navigate,
  setBusy,
  setFlash,
}) {
  const [tokens, setTokens] = useState([]);
  const [tokensFetchedAt, setTokensFetchedAt] = useState(0);
  const [loading, setLoading] = useState(true);
  const [label, setLabel] = useState("Coding agent");
  const [access, setAccess] = useState(() =>
    (ROLE_RANK[user?.role] ?? 0) >= ROLE_RANK.editor ? "stage" : "read"
  );
  const [expiresDays, setExpiresDays] = useState(30);
  const [issued, setIssued] = useState(null);
  const [minting, setMinting] = useState(false);
  const [revokingId, setRevokingId] = useState("");
  const [readiness, setReadiness] = useState(null);
  const [readinessError, setReadinessError] = useState("");

  const authDisabled = authEnabled === false;
  const baseUrl =
    typeof window !== "undefined" ? window.location.origin : "http://127.0.0.1:8000";
  const privateHost =
    typeof window !== "undefined" && isPrivateHost(window.location.hostname);
  const userRank = ROLE_RANK[user?.role] ?? 0;
  const availableLevels = useMemo(
    () => ACCESS_LEVELS.filter((level) => userRank >= ROLE_RANK[level.role]),
    [userRank]
  );
  const clientSetup = useMemo(
    () => matchingClientSetup(readiness?.source_revision),
    [readiness?.source_revision]
  );

  useEffect(() => {
    if (!availableLevels.some((level) => level.value === access)) {
      setAccess(availableLevels[0]?.value || "read");
    }
  }, [access, availableLevels]);

  useEffect(() => {
    let canceled = false;
    authGateway
      .getSetupReadiness({ token })
      .then((value) => {
        if (!canceled) {
          setReadiness(value);
          setReadinessError("");
        }
      })
      .catch((err) => {
        if (!canceled) {
          setReadiness(null);
          setReadinessError(err.message || "Setup readiness check failed.");
        }
      });
    return () => {
      canceled = true;
    };
  }, [token]);

  const refresh = useCallback(async () => {
    if (authDisabled) {
      setLoading(false);
      return;
    }
    setLoading(true);
    try {
      const page = await authGateway.listPersonalAccessTokens({ token });
      setTokens(page.data);
      setTokensFetchedAt(Date.now());
    } catch (err) {
      setFlash("", err.message || "Failed to load agent tokens.");
    } finally {
      setLoading(false);
    }
  }, [authDisabled, setFlash, token]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  async function copyText(text, successMessage) {
    if (typeof navigator !== "undefined" && navigator.clipboard) {
      try {
        await navigator.clipboard.writeText(text);
        setFlash(successMessage);
        return;
      } catch {
        // fall through to the manual-copy hint below
      }
    }
    setFlash("", "Copy failed — select the text and copy it manually.");
  }

  async function handleMint(event) {
    event.preventDefault();
    const trimmedLabel = label.trim();
    if (!trimmedLabel) {
      setFlash("", "Token label is required.");
      return;
    }
    const level = ACCESS_LEVELS.find((candidate) => candidate.value === access);
    if (!level) {
      setFlash("", "Pick an access level.");
      return;
    }
    setMinting(true);
    setBusy(true);
    setFlash("", "");
    try {
      const issuedToken = await authGateway.createPersonalAccessToken(
        {
          expires_at: new Date(
            Date.now() + Number(expiresDays) * DAY_MS - EXPIRY_SKEW_MARGIN_MS
          ).toISOString(),
          label: trimmedLabel,
          read_only: level.readOnly,
          role: level.role,
          scope: level.scope || "all",
        },
        { token }
      );
      setIssued(issuedToken);
      await refresh();
      setFlash(`Token "${issuedToken.label}" created. It is shown only once — copy it now.`);
    } catch (err) {
      setFlash("", err.message || "Failed to create the token.");
    } finally {
      setMinting(false);
      setBusy(false);
    }
  }

  async function handleRevoke(tokenId) {
    setRevokingId(tokenId);
    setFlash("", "");
    try {
      await authGateway.revokePersonalAccessToken(tokenId, { token });
      setIssued((current) => (current?.token_id === tokenId ? null : current));
      setFlash("Token revoked.");
      await refresh();
    } catch (err) {
      setFlash("", err.message || "Failed to revoke the token.");
    } finally {
      setRevokingId("");
    }
  }

  const projectId = selectedProject?.project_id || "";
  const commands = issued
    ? connectCommands(baseUrl, projectId)
    : null;
  const issuedRepoCommands = issued
    ? repoCommands(selectedProject, issued.read_only === false)
    : [];
  const openRepoCommands = repoCommands(selectedProject, true);
  const openConnectCommand =
    `lt setup connect --base-url ${baseUrl}` +
    (projectId ? ` --project ${projectId}` : "") +
    " --yes";
  const mcpCommands = codexMcpCommands(clientSetup);
  const installCommands = clientSetup
    ? [
        {
          command: clientSetup.toolInstallCommand,
          title: `Install matching lt and lt-mcp (${clientSetup.revision.slice(0, 12)})`,
        },
        {
          command: clientSetup.projectInstallCommand,
          title: "Add matching dependency in this Python project",
        },
        {
          command: clientSetup.projectImportCommand,
          title: "Verify lab_tracker_client imports in the project environment",
        },
        {
          command: clientSetup.verifyClientCommand,
          title: "Verify the project package matches this server",
        },
      ]
    : [];

  return (
    <article className="card span-12">
      <div className="item-head">
        <div>
          <h2>Agent access</h2>
          <p className="subtle">
            Mint a personal access token for a coding agent, then paste one command
            block on the machine where the agent runs. Tokens act on your behalf —
            AI proposes; only a person commits.
          </p>
        </div>
        <button type="button" className="btn-secondary" onClick={() => navigate("/app")}>
          Workspace
        </button>
      </div>

      {privateHost ? (
        <p className="warn">
          This address ({baseUrl}) looks machine-local or private. Agents on other
          computers should use your server&#39;s public URL instead — edit the copied
          commands accordingly.
        </p>
      ) : null}

      <p className="subtle">
        {selectedProject ? (
          <>
            Repository setup will bind directly to <strong>{selectedProject.name}</strong> (
            <code>{selectedProject.project_id}</code>).
          </>
        ) : (
          <>
            No setup project is selected. Return to <strong>Setup</strong> and choose a
            project before installing repository binding or commit capture.
          </>
        )}
      </p>
      <p className="subtle">
        Server-side AI drafting uses the Lab Tracker operator’s configured provider
        credential. You do not need to enter an OpenAI key locally for Lab Tracker.
      </p>
      {readinessError ? (
        <p className="warn">
          This server’s matching client revision could not be checked (
          {readinessError}). Do not install from a moving branch.
        </p>
      ) : readiness && !clientSetup ? (
        <p className="warn">
          Matching client installation is unavailable because this deployment does
          not report a full immutable source revision. Do not install from GitHub{" "}
          <code>main</code>; ask the Lab Tracker operator to deploy with revision
          metadata.
        </p>
      ) : null}

      {authDisabled ? (
        <div className="card-inset">
          <h3>Authentication is disabled on this server</h3>
          <p className="subtle">
            Agents can connect without a token. On the agent&#39;s machine, save the
            connection profile, then scaffold each analysis repo.
          </p>
          {clientSetup ? (
            <>
              {installCommands.map((item) => (
                <CommandSnippet
                  key={item.command}
                  title={item.title}
                  command={item.command}
                  onCopy={() =>
                    copyText(item.command, `${item.title} command copied.`)
                  }
                />
              ))}
              <CommandSnippet
                title="Connect this machine"
                command={openConnectCommand}
                onCopy={() =>
                  copyText(openConnectCommand, "Connect command copied.")
                }
              />
              {openRepoCommands.map((item) => (
                <CommandSnippet
                  key={item.command}
                  title={item.title}
                  command={item.command}
                  onCopy={() =>
                    copyText(item.command, `${item.title} command copied.`)
                  }
                />
              ))}
              {mcpCommands.map((item) => (
                <CommandSnippet
                  key={item.command}
                  title={item.title}
                  command={item.command}
                  onCopy={() =>
                    copyText(item.command, `${item.title} command copied.`)
                  }
                />
              ))}
              <p className="subtle">
                The pinned tool and Python dependency come from the same
                immutable revision as this server. The repo setup installs the
                Lab Tracker setup skill into the Claude and Codex user skill
                homes and writes repo-level MCP configuration. The MCP verifier
                launches <code>lt-mcp</code>, checks health, and makes a project
                read; <code>codex mcp list</code> alone checks registration.
              </p>
            </>
          ) : (
            <p className="warn">
              Local connection and repository commands are withheld until this
              deployment reports a valid immutable source revision.
            </p>
          )}
        </div>
      ) : (
        <>
          <form className="form" onSubmit={handleMint}>
            <div className="inline">
              <label>
                Label
                <input
                  value={label}
                  onChange={(event) => setLabel(event.target.value)}
                  maxLength={150}
                  placeholder="Coding agent on my laptop"
                />
              </label>
              <label>
                Access
                <select value={access} onChange={(event) => setAccess(event.target.value)}>
                  {availableLevels.map((level) => (
                    <option key={level.value} value={level.value}>
                      {level.label}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                Expires in
                <select
                  value={expiresDays}
                  onChange={(event) => setExpiresDays(Number(event.target.value))}
                >
                  {EXPIRY_CHOICES.map((choice) => (
                    <option key={choice.days} value={choice.days}>
                      {choice.label}
                    </option>
                  ))}
                </select>
              </label>
              <button type="submit" className="btn-primary" disabled={minting}>
                {minting ? "Creating…" : "Create agent token"}
              </button>
            </div>
            <p className="subtle">
              {ACCESS_LEVELS.find((level) => level.value === access)?.description}{" "}
              Shorter expirations are safer; you can mint a fresh token any time.
            </p>
          </form>

          {issued ? (
            <div className="card-inset agent-token-issued">
              <h3>Token created — shown only once</h3>
              <p className="subtle">
                Keep this one-time token private and copy it now. Run the matching
                setup block, then paste the token at its hidden prompt. The block
                saves the server URL and token in the permission-hardened Lab
                Tracker connection profile (
                <code>~/.lab-tracker/config.json</code>) used by both <code>lt</code>{" "}
                and <code>lt-mcp</code>, then clears the temporary shell value.
              </p>
              <div className="enrollment-url">
                <code>{issued.secret}</code>
                <button
                  type="button"
                  className="btn-secondary"
                  onClick={() => copyText(issued.secret, "Token copied.")}
                >
                  Copy token
                </button>
              </div>
              {clientSetup ? (
                <>
                  {installCommands.map((item) => (
                    <CommandSnippet
                      key={item.command}
                      title={item.title}
                      command={item.command}
                      onCopy={() =>
                        copyText(
                          item.command,
                          `${item.title} command copied.`
                        )
                      }
                    />
                  ))}
                  <CommandSnippet
                    title="Windows (PowerShell)"
                    command={commands.powershell}
                    onCopy={() =>
                      copyText(
                        commands.powershell,
                        "PowerShell setup commands copied."
                      )
                    }
                  />
                  <CommandSnippet
                    title="macOS / Linux"
                    command={commands.posix}
                    onCopy={() =>
                      copyText(
                        commands.posix,
                        "Shell setup commands copied."
                      )
                    }
                  />
                  {issuedRepoCommands.map((item) => (
                    <CommandSnippet
                      key={item.command}
                      title={item.title}
                      command={item.command}
                      onCopy={() =>
                        copyText(
                          item.command,
                          `${item.title} command copied.`
                        )
                      }
                    />
                  ))}
                  {mcpCommands.map((item) => (
                    <CommandSnippet
                      key={item.command}
                      title={item.title}
                      command={item.command}
                      onCopy={() =>
                        copyText(
                          item.command,
                          `${item.title} command copied.`
                        )
                      }
                    />
                  ))}
                  <p className="subtle">
                    The exact project id is included in the connection profile,
                    repository binding, and hook commands. Repo setup installs
                    the skill into both Claude and Codex user homes and writes{" "}
                    <code>.mcp.json</code>, <code>.cursor/mcp.json</code>,{" "}
                    <code>.gemini/settings.json</code>, and agent instruction
                    files. <code>lt setup status</code> reports the final local
                    state.
                  </p>
                </>
              ) : (
                <p className="warn">
                  Copy the token now, but do not connect a local client yet.
                  Setup commands are withheld until the deployment reports a
                  valid immutable source revision.
                </p>
              )}
              {issued.read_only !== false ? (
                <p className="warn">
                  This token can read and bind the selected project, but it cannot
                  sync commit-hook or figure-capture evidence. Mint{" "}
                  <strong>Read + stage evidence</strong> for capture workflows.
                </p>
              ) : null}
              <button type="button" className="btn-link" onClick={() => setIssued(null)}>
                Dismiss
              </button>
            </div>
          ) : null}

          {loading ? (
            <p className="subtle">Loading agent tokens…</p>
          ) : tokens.length === 0 ? (
            <p className="subtle">No agent tokens yet.</p>
          ) : (
            <ul className="list-clean">
              {tokens.map((item) => {
                const expired =
                  item.expires_at &&
                  new Date(item.expires_at).getTime() < tokensFetchedAt;
                return (
                  <li key={item.token_id} className="row-between">
                    <div>
                      <strong>{item.label}</strong>
                      <div className="subtle">
                        {item.role}
                        {item.read_only ? " · read-only" : " · read-write"}
                        {` · expires ${formatDate(item.expires_at)}`}
                        {item.last_used_at
                          ? ` · last used ${formatDate(item.last_used_at)}`
                          : " · not yet used"}
                        {item.revoked_at ? " · revoked" : expired ? " · expired" : ""}
                      </div>
                    </div>
                    {!item.revoked_at ? (
                      <button
                        type="button"
                        className="btn-danger"
                        disabled={revokingId === item.token_id}
                        onClick={() => handleRevoke(item.token_id)}
                      >
                        {revokingId === item.token_id ? "Revoking…" : "Revoke"}
                      </button>
                    ) : null}
                  </li>
                );
              })}
            </ul>
          )}
        </>
      )}
    </article>
  );
}

export { AgentAccessPage };
