import * as React from "react";

import { apiRequest } from "../../shared/api.js";
import { formatDate } from "../../shared/formatters.js";
import { AppLink } from "../../shared/routing.jsx";

const SOURCES = { ics: "Instrument calendar", store_scan: "Shared folder" };
const REASONS = {
  needs_session: "No session has been explicitly linked.",
  ambiguous_session: "More than one session is linked.",
  needs_operator: "A reviewer needs to be assigned.",
  operator_unavailable: "The assigned reviewer no longer has project access.",
};

async function listReviewers(projectId, token) {
  const members = await apiRequest(`/projects/${projectId}/members?limit=200`, { token });
  return members.filter(({ role }) => role === "contributor" || role === "owner");
}

function OperatorField({ members, userId, onChange, busy }) {
  return (
    <label>
      Review operator
      <select value={userId} onChange={(event) => onChange(event.target.value)} disabled={busy}>
        <option value="">Unassigned</option>
        {members.map((member) => (
          <option key={member.user_id} value={member.user_id}>{member.username || member.user_id}</option>
        ))}
      </select>
    </label>
  );
}

function OperatorForm({ token, path, members, userId = "", allowUnassigned, onSave }) {
  const [choice, setChoice] = React.useState(userId);
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState("");

  async function save(event) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      await apiRequest(path, {
        token,
        method: "PUT",
        body: { user_id: choice || null },
      });
      onSave();
    } catch (err) {
      setError(err.message || "The review operator could not be saved.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="stack" onSubmit={save}>
      <OperatorField members={members} userId={choice} onChange={setChoice} busy={busy} />
      {error ? <p className="flash error" role="alert">{error}</p> : null}
      <button type="submit" className="btn-primary" disabled={busy || (!choice && !allowUnassigned)}>
        Save operator
      </button>
    </form>
  );
}

function ReviewOperatorEditor({ token, scope, workId, projectId }) {
  const [open, setOpen] = React.useState(false);
  const [data, setData] = React.useState(null);
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState("");
  const [saved, setSaved] = React.useState(false);
  const path = `/review-operator/${scope}/${workId}`;

  async function toggle() {
    setOpen(!open);
    if (open) return;
    setError("");
    setBusy(true);
    setSaved(false);
    try {
      const [{ assignment }, members] = await Promise.all([
        apiRequest(path, { token }),
        listReviewers(projectId, token),
      ]);
      setData({ assignment, members });
    } catch (err) {
      setError(err.message || "Review operators could not be loaded.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="stack">
      <button type="button" className="btn-secondary" onClick={toggle} aria-expanded={open} disabled={busy}>
        Assign review operator
      </button>
      {saved ? <p role="status">Review operator saved.</p> : null}
      {open && error ? <p className="flash error" role="alert">{error}</p> : null}
      {open && data && !busy && !error ? (
        <OperatorForm
          token={token}
          path={path}
          members={data.members}
          userId={data.assignment?.user_id || ""}
          allowUnassigned
          onSave={() => { setOpen(false); setSaved(true); }}
        />
      ) : null}
    </section>
  );
}

function UnassignedCaptures({ token, projectId, navigate }) {
  const [open, setOpen] = React.useState(false);
  const [data, setData] = React.useState(null);
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState("");
  const [saved, setSaved] = React.useState(false);

  async function toggle() {
    setOpen(!open);
    if (open) return;
    setSaved(false);
    setError("");
    setBusy(true);
    try {
      const [items, members] = await Promise.all([
        apiRequest(`/projects/${projectId}/unassigned-machine-captures`, { token }),
        listReviewers(projectId, token),
      ]);
      setData({ items, members });
    } catch (err) {
      setError(err.message || "The unassigned captures could not be loaded.");
    } finally {
      setBusy(false);
    }
  }

  function removeAssigned(noteId) {
    setSaved(true);
    setData((current) => ({ ...current, items: current.items.filter((item) => item.note_id !== noteId) }));
  }

  return (
    <section className="stack">
      <button type="button" className="btn-secondary" onClick={toggle} aria-expanded={open} disabled={busy}>
        Unassigned machine captures
      </button>
      {open && saved ? <p role="status">Capture assigned for review.</p> : null}
      {open && error ? <p className="flash error" role="alert">{error}</p> : null}
      {open && data && !busy && !error ? (
        data.items.length ? data.items.map(({ note_id: noteId, title, source, captured_at: capturedAt, route, clock_sessions: clockSessions = [] }) => (
          <div className="stack" key={noteId}>
            <AppLink to={`/app/notes/${noteId}`} navigate={navigate}>
              {title || "Machine capture"}
            </AppLink>
            <p className="subtle">{SOURCES[source] || source} · {formatDate(capturedAt)} · {REASONS[route.status]}</p>
            {clockSessions.length ? (
              <p className="subtle">{clockSessions.length} {clockSessions.length === 1 ? "session overlaps" : "sessions overlap"} the capture time.</p>
            ) : null}
            {route.session_ids.length || clockSessions.length ? (
              <ul>
                {[...new Set([...route.session_ids, ...clockSessions])].map((id) => (
                  <li key={id}><AppLink to={`/app/sessions/${id}`} navigate={navigate}>Session {id.slice(0, 8)}</AppLink></li>
                ))}
              </ul>
            ) : null}
            <OperatorForm
              token={token}
              path={`/review-operator/note/${noteId}`}
              members={data.members}
              onSave={() => removeAssigned(noteId)}
            />
          </div>
        )) : <p className="subtle">Every staged machine capture has a reviewer.</p>
      ) : null}
    </section>
  );
}

export { ReviewOperatorEditor, UnassignedCaptures };
