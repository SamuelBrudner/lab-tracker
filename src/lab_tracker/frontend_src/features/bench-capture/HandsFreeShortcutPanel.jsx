import * as React from "react";

import { auth as authGateway } from "../../shared/gateways/index.js";
import { appBasePath } from "../../shared/routing.jsx";

const { useState } = React;

const VOICE_CAPTURE_PATH = "/notes/voice-capture";
const DEFAULT_CREDENTIAL_LABEL = "Hands-free shortcut";

function voiceCaptureEndpoint() {
  return `${window.location.origin}${appBasePath()}${VOICE_CAPTURE_PATH}`;
}

function shortcutUrl(projectId) {
  if (!projectId) {
    return "";
  }
  const params = new URLSearchParams({ project_id: projectId, session_id: "latest" });
  return `${voiceCaptureEndpoint()}?${params.toString()}`;
}

async function copyToClipboard(text) {
  if (typeof navigator === "undefined" || !navigator.clipboard) {
    return false;
  }
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    return false;
  }
}

/**
 * "Hands-free shortcut": the exact request an iOS Shortcut or an Android
 * automation app sends to stage a voice memo (POST the recording as the raw
 * body to /notes/voice-capture). The credential is a paired-device grant made
 * with the same enrollment flow a phone uses, shown once here because this
 * page minted it; it is listed with the paired devices and revoked the same
 * way. The page never shows any other secret.
 */
function HandsFreeShortcutPanel({
  token,
  canWrite,
  projects = [],
  selectedProjectId = "",
  setFlash,
  onCredentialCreated = null,
}) {
  const [projectChoice, setProjectChoice] = useState("");
  const [label, setLabel] = useState(DEFAULT_CREDENTIAL_LABEL);
  const [issued, setIssued] = useState(null);
  const [minting, setMinting] = useState(false);
  const projectId =
    projectChoice || (projects.some((item) => item.project_id === selectedProjectId)
      ? selectedProjectId
      : projects[0]?.project_id || "");
  const url = shortcutUrl(projectId);

  async function copy(text, successMessage) {
    if (await copyToClipboard(text)) {
      setFlash(successMessage);
    } else {
      setFlash("", "Copy failed; select the text and copy it manually.");
    }
  }

  async function createCredential() {
    const trimmed = label.trim() || DEFAULT_CREDENTIAL_LABEL;
    setMinting(true);
    setFlash("", "");
    try {
      // The same two steps as pairing a phone: an offer from this signed-in
      // session, then consuming it for a named device grant.
      const offer = await authGateway.createDeviceEnrollment({}, { token });
      const device = await authGateway.consumeDeviceEnrollment({
        label: trimmed,
        offer_token: offer.offer_token,
      });
      setIssued(device);
      setFlash(`Shortcut credential "${device.label}" created. It is shown only once; copy it now.`);
      await onCredentialCreated?.();
    } catch (err) {
      setFlash("", err.message || "Failed to create the shortcut credential.");
    } finally {
      setMinting(false);
    }
  }

  return (
    <section className="card-inset stack hands-free-shortcut" aria-labelledby="hands-free-title">
      <h3 id="hands-free-title">Hands-free shortcut</h3>
      <p className="subtle">
        Record a voice memo from a phone shortcut (&ldquo;Hey Siri, lab note&rdquo;, a home-screen
        button, or an Android automation) without opening the app. The memo lands as a staged
        voice note in your most recent active session, for review like any other capture.
      </p>

      <label>
        Project
        <select value={projectId} onChange={(event) => setProjectChoice(event.target.value)}>
          {projects.length === 0 ? <option value="">No projects yet</option> : null}
          {projects.map((project) => (
            <option key={project.project_id} value={project.project_id}>
              {project.name}
            </option>
          ))}
        </select>
      </label>

      <dl className="stack shortcut-request">
        <dt className="subtle">Method</dt>
        <dd className="mono">POST</dd>
        <dt className="subtle">URL</dt>
        <dd className="enrollment-url">
          <code>{url || "Choose a project first"}</code>
          {url ? (
            <button
              type="button"
              className="btn-secondary"
              onClick={() => copy(url, "Shortcut URL copied.")}
            >
              Copy URL
            </button>
          ) : null}
        </dd>
        <dt className="subtle">Headers</dt>
        <dd className="mono">
          Authorization: Bearer &lt;shortcut credential&gt;
          <br />
          Content-Type: audio/mp4
        </dd>
        <dt className="subtle">Body</dt>
        <dd>The recorded audio file itself (not a form).</dd>
      </dl>
      <p className="subtle">
        <span className="mono">session_id=latest</span> is your most recently started active
        session in the project; with none active the memo arrives without a session. Replace it
        with a session id to pin one session. Optional: <span className="mono">hint=</span> (a
        few words for transcription) and <span className="mono">captured_at=</span> (the
        phone&apos;s ISO 8601 time). Other Content-Types work too: any{" "}
        <span className="mono">audio/*</span> type, or{" "}
        <span className="mono">application/octet-stream</span> with{" "}
        <span className="mono">filename=memo.m4a</span>.
      </p>
      <details>
        <summary>Set it up</summary>
        <ol className="stack">
          <li>
            iPhone (Shortcuts app): add <strong>Record Audio</strong>, then{" "}
            <strong>Get Contents of URL</strong> with the URL above, Method POST, the two
            headers, and Request Body <strong>File</strong> set to the recorded audio. Name the
            shortcut (e.g. &ldquo;Lab note&rdquo;) to start it by voice.
          </li>
          <li>
            Android (Tasker): <strong>Record Audio</strong> to a file (MPEG4 / AAC), then{" "}
            <strong>HTTP Request</strong>, Method POST, the URL and headers above, and{" "}
            <strong>File To Send</strong> set to that file. HTTP Shortcuts and similar apps
            work the same way with a raw file body.
          </li>
        </ol>
      </details>

      <div className="stack">
        <label>
          Credential name
          <input
            type="text"
            value={label}
            maxLength={80}
            onChange={(event) => setLabel(event.target.value)}
          />
        </label>
        <p className="subtle">
          The shortcut needs its own paired-device credential. Like a paired phone it can read
          your projects and stage captures as you, and nothing else; revoke it below at any
          time.
        </p>
        <div className="inline">
          <button
            type="button"
            className="btn-secondary"
            disabled={!canWrite || minting}
            onClick={createCredential}
          >
            {minting ? "Creating credential…" : "Create shortcut credential"}
          </button>
        </div>
      </div>

      {issued ? (
        <div className="card-inset agent-token-issued" role="region" aria-label="Shortcut credential">
          <h4>Shortcut credential created — shown only once</h4>
          <p className="subtle">
            Paste it into the shortcut&apos;s Authorization header after{" "}
            <span className="mono">Bearer </span>. Keep it only there: anyone holding it can read
            your projects and stage captures as you. It will not be shown again; if it is lost,
            revoke &ldquo;{issued.label}&rdquo; below and create a new one.
          </p>
          <div className="enrollment-url">
            <code>{issued.secret}</code>
            <button
              type="button"
              className="btn-secondary"
              onClick={() => copy(issued.secret, "Shortcut credential copied.")}
            >
              Copy credential
            </button>
          </div>
          <button type="button" className="btn-link" onClick={() => setIssued(null)}>
            I&apos;ve saved it — hide
          </button>
        </div>
      ) : null}
    </section>
  );
}

export { HandsFreeShortcutPanel, shortcutUrl, voiceCaptureEndpoint };
