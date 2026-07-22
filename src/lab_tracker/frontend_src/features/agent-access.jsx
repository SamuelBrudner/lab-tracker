import * as React from "react";

import { apiListRequest, apiRequest } from "../shared/api.js";
import { formatDate } from "../shared/formatters.js";

const { useCallback, useEffect, useState } = React;

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
    value: "read",
    label: "Read-only (recommended)",
    description: "Decision context, search, and graph reads.",
    role: "viewer",
    readOnly: true,
  },
  {
    value: "stage",
    label: "Read + stage evidence",
    description: "Also stages notes, datasets, and draft requests. Never commits.",
    role: "editor",
    readOnly: false,
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

/* No persistence lines beyond `lt setup connect --save-token` here: the saved
   profile is the one consented, permission-hardened token store. The MCP
   export is process-scoped; agents launched from other shells need it set
   where they run. */
function connectCommands(secret, baseUrl) {
  return {
    posix: [
      `export LAB_TRACKER_ACCESS_TOKEN='${secret}'`,
      `export LAB_TRACKER_MCP_API_KEY="$LAB_TRACKER_ACCESS_TOKEN"`,
      `lt setup connect --base-url ${baseUrl} --save-token --yes`,
    ].join("\n"),
    powershell: [
      `$env:LAB_TRACKER_ACCESS_TOKEN = '${secret}'`,
      `$env:LAB_TRACKER_MCP_API_KEY = $env:LAB_TRACKER_ACCESS_TOKEN`,
      `lt setup connect --base-url ${baseUrl} --save-token --yes`,
    ].join("\n"),
  };
}

/* Read-only tokens can bind to an existing project but the server forbids
   them from creating one, so only offer --create when the token can write. */
function repoCommands(canCreateProjects) {
  return [
    "lt setup init --yes",
    canCreateProjects
      ? 'lt project bind --name "My project" --create --yes'
      : 'lt project bind --name "My project" --yes',
  ].join("\n");
}

const INSTALL_COMMAND = "uv tool install git+https://github.com/SamuelBrudner/lab-tracker";

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

function AgentAccessPage({ token, user, authEnabled, navigate, setBusy, setFlash }) {
  const [tokens, setTokens] = useState([]);
  const [tokensFetchedAt, setTokensFetchedAt] = useState(0);
  const [loading, setLoading] = useState(true);
  const [label, setLabel] = useState("Coding agent");
  const [access, setAccess] = useState("read");
  const [expiresDays, setExpiresDays] = useState(30);
  const [issued, setIssued] = useState(null);
  const [minting, setMinting] = useState(false);
  const [revokingId, setRevokingId] = useState("");

  const authDisabled = authEnabled === false;
  const baseUrl =
    typeof window !== "undefined" ? window.location.origin : "http://127.0.0.1:8000";
  const privateHost =
    typeof window !== "undefined" && isPrivateHost(window.location.hostname);
  const userRank = ROLE_RANK[user?.role] ?? 0;
  const availableLevels = ACCESS_LEVELS.filter(
    (level) => userRank >= ROLE_RANK[level.role]
  );

  const refresh = useCallback(async () => {
    if (authDisabled) {
      setLoading(false);
      return;
    }
    setLoading(true);
    try {
      const page = await apiListRequest("/auth/tokens", { token });
      setTokens(page.data || []);
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
      const issuedToken = await apiRequest("/auth/tokens", {
        body: {
          expires_at: new Date(
            Date.now() + Number(expiresDays) * DAY_MS - EXPIRY_SKEW_MARGIN_MS
          ).toISOString(),
          label: trimmedLabel,
          read_only: level.readOnly,
          role: level.role,
          scope: level.scope || "all",
        },
        method: "POST",
        token,
      });
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
      await apiRequest(`/auth/tokens/${tokenId}`, {
        method: "DELETE",
        token,
      });
      setIssued((current) => (current?.token_id === tokenId ? null : current));
      setFlash("Token revoked.");
      await refresh();
    } catch (err) {
      setFlash("", err.message || "Failed to revoke the token.");
    } finally {
      setRevokingId("");
    }
  }

  const commands = issued ? connectCommands(issued.secret, baseUrl) : null;
  const issuedRepoCommands = issued ? repoCommands(issued.read_only === false) : "";
  const openRepoCommands = repoCommands(true);
  const openConnectCommand = `lt setup connect --base-url ${baseUrl} --yes`;

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

      {authDisabled ? (
        <div className="card-inset">
          <h3>Authentication is disabled on this server</h3>
          <p className="subtle">
            Agents can connect without a token. On the agent&#39;s machine, save the
            connection profile, then scaffold each analysis repo.
          </p>
          <CommandSnippet
            title="Connect this machine"
            command={openConnectCommand}
            onCopy={() => copyText(openConnectCommand, "Connect command copied.")}
          />
          <CommandSnippet
            title="Inside each analysis repo"
            command={openRepoCommands}
            onCopy={() => copyText(openRepoCommands, "Repo setup commands copied.")}
          />
          <p className="subtle">
            Missing the <code>lt</code> command? Install it once with{" "}
            <code>{INSTALL_COMMAND}</code>.
          </p>
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
                Paste the block for the agent machine&#39;s shell. It saves the token
                for the <code>lt</code> client (<code>~/.lab-tracker/config.json</code>,
                readable in any later shell) and sets{" "}
                <code>LAB_TRACKER_MCP_API_KEY</code> for MCP agents{" "}
                <strong>launched from that same shell</strong> — agents started
                elsewhere (IDEs, new terminals) need that variable set where they
                run, for example in your MCP client&#39;s <code>env</code> block.
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
              <CommandSnippet
                title="Windows (PowerShell)"
                command={commands.powershell}
                onCopy={() => copyText(commands.powershell, "PowerShell setup commands copied.")}
              />
              <CommandSnippet
                title="macOS / Linux"
                command={commands.posix}
                onCopy={() => copyText(commands.posix, "Shell setup commands copied.")}
              />
              <CommandSnippet
                title="Then, inside each analysis repo"
                command={issuedRepoCommands}
                onCopy={() => copyText(issuedRepoCommands, "Repo setup commands copied.")}
              />
              {issued.read_only !== false ? (
                <p className="subtle">
                  <code>lt project bind</code> matches an existing project by
                  name; this read-only token cannot create projects, so create
                  them here in the web app first (or mint a read-write token).
                </p>
              ) : null}
              <p className="subtle">
                Missing the <code>lt</code> command? Install it once with{" "}
                <code>{INSTALL_COMMAND}</code>. The repo scaffold writes{" "}
                <code>.mcp.json</code>, <code>.cursor/mcp.json</code>,{" "}
                <code>.gemini/settings.json</code>, and agent instruction files.
              </p>
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
