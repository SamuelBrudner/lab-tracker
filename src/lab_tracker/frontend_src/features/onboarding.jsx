import * as React from "react";

import { apiRequest } from "../shared/api.js";
import { auth as authGateway } from "../shared/gateways/index.js";
import { matchingClientSetup } from "./client-setup.js";
import { DailyReviewScheduleForm } from "./daily-review-schedule.jsx";

const { useEffect, useMemo, useState } = React;

function SetupCommand({ command, label, setFlash }) {
  async function copyCommand() {
    if (typeof navigator !== "undefined" && navigator.clipboard) {
      try {
        await navigator.clipboard.writeText(command);
        setFlash(`${label} copied.`);
        return;
      } catch {
        // The command remains selectable when clipboard access is unavailable.
      }
    }
    setFlash("", "Copy failed — select the command and copy it manually.");
  }

  return (
    <div className="command-snippet">
      <div className="row-between">
        <strong>{label}</strong>
        <button type="button" className="btn-secondary" onClick={copyCommand}>
          Copy
        </button>
      </div>
      <pre className="command-block">
        <code>{command}</code>
      </pre>
    </div>
  );
}

function ReadinessStatus({ readiness, error }) {
  if (error) {
    return (
      <p className="warn">
        Runtime readiness could not be checked. Your timing can still be saved;
        ask the host operator to verify the scheduler and draft provider.
      </p>
    );
  }
  if (!readiness) {
    return <p className="subtle">Checking this server’s automation runtime…</p>;
  }

  const schedulerReady =
    readiness.scheduler_enabled && readiness.background_worker_enabled;
  const automationReady =
    schedulerReady && readiness.provider_credential_configured;
  return (
    <div className="stack setup-readiness" aria-label="Automation readiness">
      <div className="row-between">
        <span>Scheduled background worker</span>
        <span className="pill">{schedulerReady ? "Ready" : "Needs operator setup"}</span>
      </div>
      <div className="row-between">
        <span>Draft provider ({readiness.provider})</span>
        <span className="pill">
          {readiness.provider_credential_configured
            ? "Connected"
            : "Needs operator setup"}
        </span>
      </div>
      <p className={automationReady ? "subtle" : "warn"}>
        {automationReady
          ? "Automatic drafting is ready. Reviews still require a person to accept changes."
          : "Your review time will be saved, but automatic drafting is not ready until the host operator completes the items above."}
      </p>
    </div>
  );
}

function OnboardingPage({
  token,
  user,
  projects,
  selectedProjectId,
  setSelectedProjectId,
  refreshProjects,
  canWrite,
  canManageSchedule,
  navigate,
  setBusy,
  setFlash,
}) {
  const [projectName, setProjectName] = useState("");
  const [projectDescription, setProjectDescription] = useState("");
  const [creatingProject, setCreatingProject] = useState(false);
  const [scheduleSaved, setScheduleSaved] = useState(false);
  const [readiness, setReadiness] = useState(null);
  const [readinessError, setReadinessError] = useState("");

  const activeProject = useMemo(
    () => projects.find((project) => project.project_id === selectedProjectId) || null,
    [projects, selectedProjectId]
  );
  const clientSetup = useMemo(
    () => matchingClientSetup(readiness?.source_revision),
    [readiness?.source_revision]
  );

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
          setReadinessError(err.message || "Readiness check failed.");
        }
      });
    return () => {
      canceled = true;
    };
  }, [token]);

  async function createProject(event) {
    event.preventDefault();
    if (!canWrite) {
      return;
    }
    if (!projectName.trim()) {
      setFlash("", "Project name is required.");
      return;
    }

    setCreatingProject(true);
    setBusy(true);
    setFlash("", "");
    try {
      const created = await apiRequest("/projects", {
        body: {
          description: projectDescription.trim() || null,
          name: projectName.trim(),
        },
        method: "POST",
        token,
      });
      await refreshProjects();
      setSelectedProjectId(created.project_id);
      setProjectName("");
      setProjectDescription("");
      setScheduleSaved(false);
      setFlash("Project created. Next, choose your daily review time.");
    } catch (err) {
      setFlash("", err.message || "Failed to create project.");
    } finally {
      setCreatingProject(false);
      setBusy(false);
    }
  }

  const repoSetupCommands = activeProject
    ? [
        {
          command: "lt setup init --install-skills --dry-run",
          label: "1. Preview repository and skill setup",
        },
        {
          command: "lt setup init --install-skills --yes",
          label: "2. Apply after reviewing the preview",
        },
        {
          command:
            `lt project bind --project-id ${activeProject.project_id} --dry-run`,
          label: `3. Preview binding to ${activeProject.name}`,
        },
        {
          command: `lt project bind --project-id ${activeProject.project_id} --yes`,
          label: `4. Bind to ${activeProject.name}`,
        },
        {
          command:
            `lt hooks install --project ${activeProject.project_id} --dry-run`,
          label: "5. Preview commit capture hook",
        },
        {
          command: `lt hooks install --project ${activeProject.project_id} --yes`,
          label: "6. Install commit capture hook",
        },
        {
          command: "lt setup status",
          label: "7. Verify repository setup",
        },
      ]
    : [];

  return (
    <article className="card span-12 setup-page">
      <div className="item-head">
        <div>
          <p className="eyebrow">Guided setup</p>
          <h2>Set up your Lab Tracker</h2>
          <p className="subtle">
            Welcome{user?.username ? `, ${user.username}` : ""}. This checklist connects
            one research project, your review rhythm, and the agents you use for analysis.
            You can reopen it from <strong>Setup</strong> at any time.
          </p>
        </div>
        <button type="button" className="btn-secondary" onClick={() => navigate("/app")}>
          Skip for now
        </button>
      </div>

      <ol className="setup-steps">
        <li className="card-inset setup-step">
          <div className="item-head">
            <div>
              <p className="eyebrow">Step 1</p>
              <h3>Choose the research project</h3>
            </div>
            <span className="pill">{activeProject ? "Selected" : "Required"}</span>
          </div>

          {projects.length > 0 ? (
            <label>
              Setup project
              <select
                value={selectedProjectId}
                onChange={(event) => {
                  setSelectedProjectId(event.target.value);
                  setScheduleSaved(false);
                }}
              >
                {projects.map((project) => (
                  <option key={project.project_id} value={project.project_id}>
                    {project.name}
                  </option>
                ))}
              </select>
            </label>
          ) : (
            <p className="subtle">No projects are available to this account yet.</p>
          )}

          {canWrite ? (
            <details open={projects.length === 0}>
              <summary>{projects.length === 0 ? "Create the first project" : "Create another project"}</summary>
              <form className="form setup-project-form" onSubmit={createProject}>
                <label>
                  Project name
                  <input
                    value={projectName}
                    maxLength={200}
                    onChange={(event) => setProjectName(event.target.value)}
                    placeholder="e.g. Odor-guided navigation"
                  />
                </label>
                <label>
                  Short description
                  <textarea
                    value={projectDescription}
                    onChange={(event) => setProjectDescription(event.target.value)}
                    placeholder="What question or system does this project track?"
                  />
                </label>
                <button type="submit" className="btn-primary" disabled={creatingProject}>
                  {creatingProject ? "Creating…" : "Create project"}
                </button>
              </form>
            </details>
          ) : projects.length === 0 ? (
            <p className="warn">
              Ask an administrator to add you to a project before continuing.
            </p>
          ) : null}
        </li>

        <li className="card-inset setup-step">
          <div className="item-head">
            <div>
              <p className="eyebrow">Step 2</p>
              <h3>Set your daily review timing</h3>
            </div>
            <span className="pill">{scheduleSaved ? "Saved" : "Personal schedule"}</span>
          </div>
          <p className="subtle">
            Lab Tracker can draft a review queue around your workday. This schedule belongs
            to your account for the selected project; it does not overwrite a colleague’s
            timing.
          </p>
          {activeProject && canManageSchedule ? (
            <DailyReviewScheduleForm
              token={token}
              projectId={activeProject.project_id}
              canManage={canManageSchedule}
              setBusy={setBusy}
              setFlash={setFlash}
              onSaved={() => setScheduleSaved(true)}
            />
          ) : activeProject ? (
            <p className="warn">
              Your project role cannot change review timing. Ask a project owner or
              administrator to enable it.
            </p>
          ) : (
            <p className="subtle">Choose or create a project first.</p>
          )}
          <ReadinessStatus readiness={readiness} error={readinessError} />
        </li>

        <li className="card-inset setup-step">
          <div className="item-head">
            <div>
              <p className="eyebrow">Step 3</p>
              <h3>Connect your coding agent</h3>
            </div>
            <span className="pill">On your computer</span>
          </div>
          <p>
            Install the client revision that matches this server, then open{" "}
            <strong>Agents</strong> here to create a personal token. The one-time
            connection command saves one local profile used by both <code>lt</code>{" "}
            and <code>lt-mcp</code>.
          </p>
          {clientSetup ? (
            <>
              <SetupCommand
                label={`Install matching lt and lt-mcp (${clientSetup.revision.slice(0, 12)})`}
                command={clientSetup.toolInstallCommand}
                setFlash={setFlash}
              />
              <p className="subtle">
                This exact Git revision matches the running server. It does not track
                a moving branch. If <code>uv --version</code> is unavailable, install{" "}
                <a href="https://docs.astral.sh/uv/getting-started/installation/">
                  uv from its official installation guide
                </a>{" "}
                first.
              </p>
            </>
          ) : readinessError ? (
            <p className="warn">
              This server’s matching client revision could not be checked (
              {readinessError}). Do not install from a moving branch.
            </p>
          ) : readiness ? (
            <p className="warn">
              Matching client installation is unavailable because this deployment
              does not report a full immutable source revision. Do not install from
              GitHub <code>main</code>; ask the Lab Tracker operator to deploy with
              revision metadata.
            </p>
          ) : (
            <p className="subtle">Waiting for this server’s client revision…</p>
          )}
          <p className="subtle">
            Server-side AI drafting uses the Lab Tracker operator’s configured
            provider credential. You do not need to enter an OpenAI key locally for
            Lab Tracker.
          </p>
          <button type="button" className="btn-primary" onClick={() => navigate("/app/agents")}>
            Create an agent token
          </button>
        </li>

        <li className="card-inset setup-step">
          <div className="item-head">
            <div>
              <p className="eyebrow">Step 4</p>
              <h3>Install Python support, skills, and commit capture</h3>
            </div>
            <span className="pill">Review before apply</span>
          </div>
          <p className="subtle">
            Run these inside each analysis repository. First add the same pinned
            package to the project’s Python environment so figure capture can import{" "}
            <code>lab_tracker_client</code>. Then inspect each dry run before its
            matching <code>--yes</code> command. The commands bind the exact project
            selected above—there is no placeholder to replace.
          </p>
          {clientSetup ? (
            <>
              <SetupCommand
                label="Add matching Lab Tracker dependency to this Python project"
                command={clientSetup.projectInstallCommand}
                setFlash={setFlash}
              />
              <SetupCommand
                label="Verify lab_tracker_client imports in the project environment"
                command={clientSetup.projectImportCommand}
                setFlash={setFlash}
              />
              <SetupCommand
                label="Verify the project package matches this server"
                command={clientSetup.verifyClientCommand}
                setFlash={setFlash}
              />
              {repoSetupCommands.map((item) => (
                <SetupCommand
                  key={item.command}
                  label={item.label}
                  command={item.command}
                  setFlash={setFlash}
                />
              ))}
              <p className="subtle">
                The skill installer covers Claude and Codex user skill homes.
                Codex usually detects skill changes automatically; use{" "}
                <code>/skills</code> to verify and restart Codex if the skill
                does not appear. Commit capture needs the{" "}
                <strong>Read + stage evidence</strong> token recommended on the
                Agents page; a read-only token cannot sync staged notes.
              </p>
            </>
          ) : (
            <p className="warn">
              Local repository commands are withheld until this deployment
              reports a valid immutable source revision.
            </p>
          )}
        </li>

        <li className="card-inset setup-step">
          <div className="item-head">
            <div>
              <p className="eyebrow">Step 5</p>
              <h3>Register and verify MCP in Codex</h3>
            </div>
            <span className="pill">Run once</span>
          </div>
          <p className="subtle">
            Repository scaffolding also writes MCP files for supported clients.
            Current Codex clients share MCP configuration through{" "}
            <code>config.toml</code>, so register the local <code>lt-mcp</code>{" "}
            server once. Then launch the executable through the setup verifier: it
            performs an MCP initialize exchange, calls health, and makes an
            authenticated project read using the saved profile.
          </p>
          {clientSetup ? (
            <>
              <SetupCommand
                label="1. Register Lab Tracker MCP for Codex"
                command="codex mcp add lab-tracker -- lt-mcp"
                setFlash={setFlash}
              />
              <SetupCommand
                label="2. Launch MCP and verify health, auth, and client revision"
                command={clientSetup.verifyMcpCommand}
                setFlash={setFlash}
              />
              <SetupCommand
                label="3. Confirm Codex registration"
                command="codex mcp list"
                setFlash={setFlash}
              />
            </>
          ) : (
            <p className="warn">
              MCP verification is blocked until the server reports its immutable
              source revision.
            </p>
          )}
          <p className="subtle">
            In Codex, <code>/mcp</code> shows connected servers. If your organization
            manages MCP policy, an administrator may need to allow this server.
          </p>
        </li>

        <li className="card-inset setup-step">
          <div className="item-head">
            <div>
              <p className="eyebrow">Optional</p>
              <h3>Add fast capture from your phone</h3>
            </div>
          </div>
          <p className="subtle">
            Pair a phone for quick notes, observations, and uploads at the bench.
          </p>
          <button type="button" className="btn-secondary" onClick={() => navigate("/app/devices")}>
            Set up a device
          </button>
        </li>
      </ol>

      <div className="row-between setup-finish">
        <p className="subtle">
          Setup never commits research records automatically. Agents stage proposals;
          a person reviews what enters the graph.
        </p>
        <button type="button" className="btn-primary" onClick={() => navigate("/app")}>
          Finish in workspace
        </button>
      </div>
    </article>
  );
}

export { OnboardingPage, ReadinessStatus, SetupCommand };
