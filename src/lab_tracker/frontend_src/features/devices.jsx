import * as React from "react";

import { apiListRequest, apiRequest, buildApiPath } from "../shared/api.js";
import { formatDate } from "../shared/formatters.js";

const { useCallback, useEffect, useMemo, useState } = React;

function DevicesPage({
  token,
  canWrite,
  selectedProjectId = "",
  questions = [],
  datasets = [],
  sessions = [],
  navigate,
  setFlash,
}) {
  const [devices, setDevices] = useState([]);
  const [contexts, setContexts] = useState([]);
  const [loading, setLoading] = useState(true);
  const [creatingOffer, setCreatingOffer] = useState(false);
  const [creatingContext, setCreatingContext] = useState(false);
  const [creatingComputer, setCreatingComputer] = useState(false);
  const [pendingOffer, setPendingOffer] = useState(null);
  const [issuedComputer, setIssuedComputer] = useState(null);
  const [revokingId, setRevokingId] = useState("");
  const [contextLabel, setContextLabel] = useState("");
  const [siteLabel, setSiteLabel] = useState("");
  const [placeLabel, setPlaceLabel] = useState("");
  const [defaultHint, setDefaultHint] = useState("");
  const [questionId, setQuestionId] = useState("");
  const [sessionId, setSessionId] = useState("");
  const [datasetId, setDatasetId] = useState("");
  const [computerLabel, setComputerLabel] = useState(() => defaultHostLabel());
  const [computerContextId, setComputerContextId] = useState("");
  const [computerContextTouched, setComputerContextTouched] = useState(false);

  const activeContexts = useMemo(
    () => contexts.filter((context) => !context.revoked_at),
    [contexts]
  );
  const mobileDevices = useMemo(
    () => devices.filter((device) => (device.kind || "mobile") === "mobile"),
    [devices]
  );
  const computerDevices = useMemo(
    () => devices.filter((device) => device.kind === "computer"),
    [devices]
  );

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const [devicePage, contextPage] = await Promise.all([
        apiListRequest("/auth/devices", { token }),
        apiListRequest(buildApiPath("/capture-contexts", { include_revoked: true, limit: 200 }), {
          token,
        }),
      ]);
      setDevices(devicePage.data || []);
      setContexts(contextPage.data || []);
    } catch (err) {
      setFlash("", err.message || "Failed to load devices and capture setup.");
    } finally {
      setLoading(false);
    }
  }, [setFlash, token]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  useEffect(() => {
    if (!computerContextTouched && !computerContextId && activeContexts.length > 0) {
      setComputerContextId(activeContexts[0].capture_context_id);
    }
  }, [activeContexts, computerContextId, computerContextTouched]);

  function defaultTargets() {
    const targets = [];
    if (questionId) {
      targets.push({ entity_id: questionId, entity_type: "question" });
    }
    if (sessionId) {
      targets.push({ entity_id: sessionId, entity_type: "session" });
    }
    if (datasetId) {
      targets.push({ entity_id: datasetId, entity_type: "dataset" });
    }
    return targets;
  }

  async function handleCreateOffer() {
    setCreatingOffer(true);
    setFlash("", "");
    try {
      const offer = await apiRequest("/auth/devices/enrollment", {
        body: {},
        method: "POST",
        token,
      });
      setPendingOffer(offer);
    } catch (err) {
      setFlash("", err.message || "Failed to create enrollment offer.");
    } finally {
      setCreatingOffer(false);
    }
  }

  async function handleCreateContext(event) {
    event.preventDefault();
    if (!selectedProjectId || !contextLabel.trim()) {
      setFlash("", "Choose a project and name the capture QR.");
      return;
    }
    setCreatingContext(true);
    setFlash("", "");
    try {
      const context = await apiRequest("/capture-contexts", {
        body: {
          project_id: selectedProjectId,
          label: contextLabel.trim(),
          site_label: siteLabel.trim() || null,
          place_label: placeLabel.trim() || null,
          default_hint: defaultHint.trim() || null,
          default_targets: defaultTargets(),
        },
        method: "POST",
        token,
      });
      setContextLabel("");
      setSiteLabel("");
      setPlaceLabel("");
      setDefaultHint("");
      setQuestionId("");
      setSessionId("");
      setDatasetId("");
      setComputerContextId(context.capture_context_id);
      setFlash("Capture QR created.");
      await refresh();
    } catch (err) {
      setFlash("", err.message || "Failed to create capture QR.");
    } finally {
      setCreatingContext(false);
    }
  }

  async function handleCreateComputer(event) {
    event.preventDefault();
    if (!computerLabel.trim()) {
      setFlash("", "Name this computer before registering it.");
      return;
    }
    const captureContext = activeContexts.find(
      (context) => context.capture_context_id === computerContextId
    );
    const projectId = captureContext?.project_id || selectedProjectId;
    if (!projectId) {
      setFlash("", "Choose a project or capture context before registering the computer.");
      return;
    }
    setCreatingComputer(true);
    setFlash("", "");
    try {
      const issued = await apiRequest("/auth/devices", {
        body: { label: computerLabel.trim(), kind: "computer" },
        method: "POST",
        token,
      });
      setIssuedComputer({
        ...issued,
        capture_context_id: computerContextId,
        capture_host_label: computerLabel.trim(),
        project_id: projectId,
      });
      setFlash("Computer token created. The secret is shown once.");
      await refresh();
    } catch (err) {
      setFlash("", err.message || "Failed to register computer.");
    } finally {
      setCreatingComputer(false);
    }
  }

  async function handleRevoke(deviceTokenId) {
    setRevokingId(deviceTokenId);
    setFlash("", "");
    try {
      await apiRequest(`/auth/devices/${deviceTokenId}`, {
        method: "DELETE",
        token,
      });
      setFlash("Device revoked.");
      await refresh();
    } catch (err) {
      setFlash("", err.message || "Failed to revoke device.");
    } finally {
      setRevokingId("");
    }
  }

  async function handleRevokeContext(contextId) {
    setRevokingId(contextId);
    setFlash("", "");
    try {
      await apiRequest(`/capture-contexts/${contextId}`, {
        method: "DELETE",
        token,
      });
      setFlash("Capture QR revoked.");
      await refresh();
    } catch (err) {
      setFlash("", err.message || "Failed to revoke capture QR.");
    } finally {
      setRevokingId("");
    }
  }

  async function copyText(text, message) {
    if (typeof navigator !== "undefined" && navigator.clipboard) {
      try {
        await navigator.clipboard.writeText(text);
        setFlash(message);
        return;
      } catch {
        // Fall through to the visible code block.
      }
    }
    setFlash("Select and copy the command below.");
  }

  return (
    <article className="card span-12">
      <div className="item-head">
        <div>
          <h2>Devices & capture setup</h2>
          <p className="subtle">
            Pair phones, print place-aware capture QR codes, and register lab computers for
            capture-only attribution.
          </p>
        </div>
        <button type="button" className="btn-secondary" onClick={() => navigate("/app")}>
          Workspace
        </button>
      </div>

      <section className="form">
        <div className="item-head">
          <h3>Paired phones</h3>
          <button
            type="button"
            className="btn-primary"
            disabled={!canWrite || creatingOffer}
            onClick={handleCreateOffer}
          >
            {creatingOffer ? "Creating..." : "Add phone"}
          </button>
        </div>
        {pendingOffer ? (
          <div className="card-inset enrollment-offer">
            <h3>Scan with the phone camera</h3>
            <p className="subtle">
              The code expires {pendingOffer.expires_at ? formatDate(pendingOffer.expires_at) : "shortly"}.
            </p>
            {pendingOffer.enrollment_qr_svg ? (
              <div
                className="enrollment-qr"
                aria-label="Pairing QR code"
                dangerouslySetInnerHTML={{ __html: pendingOffer.enrollment_qr_svg }}
              />
            ) : null}
            <div className="enrollment-url">
              <code>{pendingOffer.enrollment_url}</code>
              <button
                type="button"
                className="btn-secondary"
                onClick={() =>
                  copyText(pendingOffer.enrollment_url, "Enrollment URL copied.")
                }
              >
                Copy URL
              </button>
            </div>
            <button type="button" className="btn-link" onClick={() => setPendingOffer(null)}>
              Dismiss
            </button>
          </div>
        ) : null}
        {loading ? (
          <p className="subtle">Loading paired phones...</p>
        ) : (
          <DeviceList
            devices={mobileDevices}
            empty="No phones paired yet."
            onRevoke={handleRevoke}
            revokingId={revokingId}
            canWrite={canWrite}
          />
        )}
      </section>

      <section className="form">
        <div className="item-head">
          <h3>Printable QR contexts</h3>
          <button type="button" className="btn-secondary" onClick={() => window.print?.()}>
            Print
          </button>
        </div>
        <form className="form" onSubmit={handleCreateContext}>
          <label>
            Label
            <input
              disabled={!canWrite}
              onChange={(event) => setContextLabel(event.target.value)}
              placeholder="Rig 2 morning captures"
              value={contextLabel}
            />
          </label>
          <div className="form-grid">
            <label>
              Site
              <input
                disabled={!canWrite}
                onChange={(event) => setSiteLabel(event.target.value)}
                placeholder="West campus"
                value={siteLabel}
              />
            </label>
            <label>
              Place
              <input
                disabled={!canWrite}
                onChange={(event) => setPlaceLabel(event.target.value)}
                placeholder="Rig 2"
                value={placeLabel}
              />
            </label>
          </div>
          <label>
            Default hint
            <input
              disabled={!canWrite}
              onChange={(event) => setDefaultHint(event.target.value)}
              placeholder="Morning behavior rig observations"
              value={defaultHint}
            />
          </label>
          <div className="form-grid">
            <label>
              Question
              <select
                disabled={!canWrite || !selectedProjectId}
                onChange={(event) => setQuestionId(event.target.value)}
                value={questionId}
              >
                <option value="">No default</option>
                {questions.map((question) => (
                  <option key={question.question_id} value={question.question_id}>
                    {compact(question.text || question.question_id)}
                  </option>
                ))}
              </select>
            </label>
            <label>
              Session
              <select
                disabled={!canWrite || !selectedProjectId}
                onChange={(event) => setSessionId(event.target.value)}
                value={sessionId}
              >
                <option value="">No default</option>
                {sessions.map((session) => (
                  <option key={session.session_id} value={session.session_id}>
                    {session.session_type} {formatDate(session.started_at)}
                  </option>
                ))}
              </select>
            </label>
            <label>
              Dataset
              <select
                disabled={!canWrite || !selectedProjectId}
                onChange={(event) => setDatasetId(event.target.value)}
                value={datasetId}
              >
                <option value="">No default</option>
                {datasets.map((dataset) => (
                  <option key={dataset.dataset_id} value={dataset.dataset_id}>
                    {compact(dataset.commit_hash || dataset.dataset_id)}
                  </option>
                ))}
              </select>
            </label>
          </div>
          <button
            type="submit"
            className="btn-primary"
            disabled={!canWrite || !selectedProjectId || !contextLabel.trim() || creatingContext}
          >
            {creatingContext ? "Creating..." : "Create printable QR"}
          </button>
        </form>

        {activeContexts.length === 0 ? (
          <p className="subtle">No printable capture QR contexts yet.</p>
        ) : (
          <div className="grid-mini">
            {activeContexts.map((context) => (
              <article className="card-inset" key={context.capture_context_id}>
                <div className="item-head">
                  <div>
                    <strong>{context.label}</strong>
                    <div className="subtle">
                      {[context.site_label, context.place_label].filter(Boolean).join(" / ")}
                    </div>
                  </div>
                  <button
                    type="button"
                    className="btn-danger"
                    disabled={!canWrite || revokingId === context.capture_context_id}
                    onClick={() => handleRevokeContext(context.capture_context_id)}
                  >
                    {revokingId === context.capture_context_id ? "Revoking..." : "Revoke"}
                  </button>
                </div>
                <div
                  className="enrollment-qr"
                  aria-label={`Capture QR for ${context.label}`}
                  dangerouslySetInnerHTML={{ __html: context.capture_qr_svg }}
                />
                <div className="enrollment-url">
                  <code>{context.capture_url}</code>
                  <button
                    type="button"
                    className="btn-secondary"
                    onClick={() => copyText(context.capture_url, "Capture URL copied.")}
                  >
                    Copy URL
                  </button>
                </div>
              </article>
            ))}
          </div>
        )}
      </section>

      <section className="form">
        <h3>Registered computers</h3>
        <form className="form" onSubmit={handleCreateComputer}>
          <label>
            Computer label
            <input
              disabled={!canWrite}
              onChange={(event) => setComputerLabel(event.target.value)}
              value={computerLabel}
            />
          </label>
          <label>
            Default QR context
            <select
              disabled={!canWrite || activeContexts.length === 0}
              onChange={(event) => {
                setComputerContextTouched(true);
                setComputerContextId(event.target.value);
              }}
              value={computerContextId}
            >
              <option value="">No default context</option>
              {activeContexts.map((context) => (
                <option key={context.capture_context_id} value={context.capture_context_id}>
                  {context.label}
                </option>
              ))}
            </select>
          </label>
          <button
            type="submit"
            className="btn-primary"
            disabled={!canWrite || !computerLabel.trim() || creatingComputer}
          >
            {creatingComputer ? "Registering..." : "Register this computer"}
          </button>
        </form>
        {issuedComputer ? (
          <ComputerCommands issued={issuedComputer} onCopy={copyText} />
        ) : null}
        <DeviceList
          devices={computerDevices}
          empty="No registered computers yet."
          onRevoke={handleRevoke}
          revokingId={revokingId}
          canWrite={canWrite}
        />
      </section>
    </article>
  );
}

function DeviceList({ devices, empty, onRevoke, revokingId, canWrite }) {
  if (devices.length === 0) {
    return <p className="subtle">{empty}</p>;
  }
  return (
    <ul className="list-clean">
      {devices.map((device) => (
        <li key={device.device_token_id} className="row-between">
          <div>
            <strong>{device.label}</strong>
            <div className="subtle">
              Registered {formatDate(device.created_at)}
              {device.last_used_at
                ? ` / last used ${formatDate(device.last_used_at)}`
                : " / not yet used"}
              {device.revoked_at ? " / revoked" : ""}
            </div>
          </div>
          {!device.revoked_at ? (
            <button
              type="button"
              className="btn-danger"
              disabled={!canWrite || revokingId === device.device_token_id}
              onClick={() => onRevoke(device.device_token_id)}
            >
              {revokingId === device.device_token_id ? "Revoking..." : "Revoke"}
            </button>
          ) : null}
        </li>
      ))}
    </ul>
  );
}

function ComputerCommands({ issued, onCopy }) {
  const commands = setupCommands(issued);
  return (
    <div className="card-inset">
      <h3>One-time setup commands</h3>
      <p className="subtle">
        Copy the token, run the command for this computer, then paste the token at the hidden
        prompt. The token is not included in the command or shell history.
      </p>
      <div className="enrollment-url">
        <code>{issued.secret}</code>
        <button
          type="button"
          className="btn-secondary"
          onClick={() => onCopy(issued.secret, "Computer token copied.")}
        >
          Copy token
        </button>
      </div>
      {commands.map((item) => (
        <div className="enrollment-url" key={item.label}>
          <code>{item.command}</code>
          <button
            type="button"
            className="btn-secondary"
            onClick={() => onCopy(item.command, `${item.label} command copied.`)}
          >
            Copy {item.label}
          </button>
        </div>
      ))}
    </div>
  );
}

function setupCommands(issued) {
  const baseUrl = typeof window === "undefined" ? "http://127.0.0.1:8000" : window.location.origin;
  const contextArg = issued.capture_context_id
    ? ` --capture-context ${shellQuote(issued.capture_context_id)}`
    : "";
  const psContextArg = issued.capture_context_id
    ? ` --capture-context ${powerShellQuote(issued.capture_context_id)}`
    : "";
  const projectArg = issued.project_id ? ` --project ${shellQuote(issued.project_id)}` : "";
  const psProjectArg = issued.project_id
    ? ` --project ${powerShellQuote(issued.project_id)}`
    : "";
  const common = [
    "lt setup connect",
    `--base-url ${shellQuote(baseUrl)}`,
    "--save-token",
    "--prompt-token",
    `${projectArg}`,
    `--capture-host ${shellQuote(issued.capture_host_label)}`,
    `${contextArg}`,
    "--yes",
  ]
    .filter(Boolean)
    .join(" ");
  const powerShell = [
    "lt setup connect",
    `--base-url ${powerShellQuote(baseUrl)}`,
    "--save-token",
    "--prompt-token",
    `${psProjectArg}`,
    `--capture-host ${powerShellQuote(issued.capture_host_label)}`,
    `${psContextArg}`,
    "--yes",
  ]
    .filter(Boolean)
    .join(" ");
  return [
    { label: "PowerShell", command: powerShell },
    { label: "macOS", command: common },
    { label: "Linux", command: common },
  ];
}

function defaultHostLabel() {
  if (typeof window === "undefined") {
    return "lab-computer";
  }
  return window.location.hostname || "lab-computer";
}

function compact(value) {
  const text = String(value || "");
  return text.length > 80 ? `${text.slice(0, 77)}...` : text;
}

function shellQuote(value) {
  return `'${String(value || "").replaceAll("'", "'\\''")}'`;
}

function powerShellQuote(value) {
  return `'${String(value || "").replaceAll("'", "''")}'`;
}

export { DevicesPage, setupCommands };
