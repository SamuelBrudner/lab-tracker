import * as React from "react";

import { apiRequest } from "../../shared/api.js";
import { formatDate } from "../../shared/formatters.js";
import { notes as notesGateway, sessions as sessionsGateway } from "../../shared/gateways/index.js";

const { useCallback, useEffect, useRef, useState } = React;

// Dismissals are per device: a suggestion id is stable on the server, so one
// key per id is enough and survives reloads without any server state.
const DISMISSED_KEY_PREFIX = "lab-tracker:session-suggestion-dismissed:";

function isDismissed(suggestionId) {
  try {
    return globalThis.localStorage?.getItem(`${DISMISSED_KEY_PREFIX}${suggestionId}`) != null;
  } catch {
    // Unavailable storage (private mode, blocked site data) just means nothing
    // was dismissed on this device.
    return false;
  }
}

function rememberDismissed(suggestionId) {
  try {
    globalThis.localStorage?.setItem(
      `${DISMISSED_KEY_PREFIX}${suggestionId}`,
      new Date().toISOString()
    );
  } catch {
    // The suggestion is still hidden for this view; it may return on reload.
  }
}

function isStartKind(kind) {
  return kind === "start_session_from_captures" || kind === "start_session_from_booking";
}

function endsInThePast(value) {
  const at = Date.parse(value || "");
  return Number.isFinite(at) && at <= Date.now();
}

function kindLabel(kind) {
  if (kind === "close_quiet_session") {
    return "quiet session";
  }
  if (kind === "start_session_from_booking") {
    return "booking";
  }
  return "sessionless captures";
}

// Attach each listed capture to the new session through the ordinary note
// route. PATCH replaces the targets list, so read the note's current targets
// first and add the session to them.
async function attachCaptures(noteIds, sessionId, token) {
  for (const noteId of noteIds) {
    const note = await notesGateway.getNote(noteId, { token });
    const current = Array.isArray(note.targets) ? note.targets : [];
    if (current.some((target) => target?.entity_type === "session" && target?.entity_id === sessionId)) {
      continue;
    }
    await apiRequest(`/notes/${encodeURIComponent(noteId)}`, {
      body: {
        targets: [
          ...current.map((target) => ({
            entity_id: target.entity_id,
            entity_type: target.entity_type,
          })),
          { entity_id: sessionId, entity_type: "session" },
        ],
      },
      method: "PATCH",
      token,
    });
  }
}

// Deterministic session bookkeeping the server noticed: an open session gone
// quiet, a bench day with no session, an instrument booking no session covers.
// Nothing is applied until the person clicks Apply, which calls the ordinary
// session routes as them (and, only on the "attach" click, the note routes).
// Dismiss hides a suggestion on this device. A failed read hides the card:
// suggestions are optional hints and must not compete with the page's errors.
function SessionSuggestionsCard({
  projectId,
  token,
  canWrite = false,
  onApplied = null,
  variant = "section",
}) {
  const [suggestions, setSuggestions] = useState([]);
  const [hiddenIds, setHiddenIds] = useState(() => new Set());
  const [pendingId, setPendingId] = useState("");
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const generationRef = useRef(0);

  const load = useCallback(async () => {
    const generation = ++generationRef.current;
    if (!projectId) {
      setSuggestions([]);
      return;
    }
    try {
      const report = await sessionsGateway.listSessionSuggestions(projectId, { token });
      if (generation === generationRef.current) {
        setSuggestions(report.suggestions || []);
      }
    } catch {
      if (generation === generationRef.current) {
        setSuggestions([]);
      }
    }
  }, [projectId, token]);

  useEffect(() => {
    setSuggestions([]);
    setMessage("");
    setError("");
    load();
    return () => {
      generationRef.current += 1;
    };
  }, [load]);

  const visible = suggestions.filter(
    (suggestion) =>
      !hiddenIds.has(suggestion.suggestion_id) && !isDismissed(suggestion.suggestion_id)
  );
  if (!projectId || (visible.length === 0 && !message && !error)) {
    return null;
  }

  function dismiss(suggestion) {
    rememberDismissed(suggestion.suggestion_id);
    setHiddenIds((current) => new Set([...current, suggestion.suggestion_id]));
  }

  async function apply(suggestion, { attach = false } = {}) {
    setPendingId(suggestion.suggestion_id);
    setMessage("");
    setError("");
    try {
      if (suggestion.kind === "close_quiet_session") {
        await sessionsGateway.updateSession(
          suggestion.session_id,
          { ended_at: suggestion.end_at, status: "closed" },
          { token }
        );
        setMessage("Session ended.");
      } else {
        const created = await sessionsGateway.createSession(
          { project_id: projectId, session_type: "operational", started_at: suggestion.start_at },
          { token }
        );
        if (suggestion.end_at && endsInThePast(suggestion.end_at)) {
          await sessionsGateway.updateSession(
            created.session_id,
            { ended_at: suggestion.end_at, status: "closed" },
            { token }
          );
        }
        if (attach) {
          await attachCaptures(suggestion.capture_note_ids || [], created.session_id, token);
        }
        setMessage(
          attach ? "Session recorded and captures attached." : "Session recorded."
        );
      }
      setHiddenIds((current) => new Set([...current, suggestion.suggestion_id]));
      onApplied?.();
      await load();
    } catch (err) {
      setError(err?.message || "Could not apply the suggestion.");
    } finally {
      setPendingId("");
    }
  }

  const body = (
    <>
      <p className="subtle">
        Noticed from your captures and bookings. Nothing changes until you apply one.
      </p>
      {message ? (
        <p className="flash ok" role="status">
          {message}
        </p>
      ) : null}
      {error ? (
        <p className="flash error" role="alert">
          {error}
        </p>
      ) : null}
      <ul className="compact-list">
        {visible.map((suggestion) => {
          const captureCount = (suggestion.capture_note_ids || []).length;
          const pending = pendingId === suggestion.suggestion_id;
          return (
            <li className="item session-suggestion" key={suggestion.suggestion_id}>
              <div className="item-head">
                <strong>{suggestion.title}</strong>
                <span className="pill">{kindLabel(suggestion.kind)}</span>
              </div>
              {suggestion.detail ? <p className="subtle">{suggestion.detail}</p> : null}
              {isStartKind(suggestion.kind) && suggestion.start_at ? (
                <p className="subtle">
                  {formatDate(suggestion.start_at)}
                  {suggestion.end_at ? ` to ${formatDate(suggestion.end_at)}` : ""}
                </p>
              ) : null}
              <div className="inline">
                <button
                  type="button"
                  className="btn-primary"
                  disabled={!canWrite || Boolean(pendingId)}
                  onClick={() => apply(suggestion)}
                >
                  {pending ? "Applying..." : "Apply"}
                </button>
                {isStartKind(suggestion.kind) && captureCount > 0 ? (
                  <button
                    type="button"
                    className="btn-secondary"
                    disabled={!canWrite || Boolean(pendingId)}
                    onClick={() => apply(suggestion, { attach: true })}
                  >
                    Apply and attach {captureCount} capture{captureCount === 1 ? "" : "s"}
                  </button>
                ) : null}
                <button
                  type="button"
                  className="btn-secondary"
                  disabled={pending}
                  onClick={() => dismiss(suggestion)}
                >
                  Dismiss
                </button>
              </div>
            </li>
          );
        })}
      </ul>
    </>
  );

  if (variant === "card") {
    return (
      <article className="card span-12" aria-label="Session suggestions">
        <h2>Session suggestions</h2>
        {body}
      </article>
    );
  }
  return (
    <section className="stack session-suggestions" aria-label="Session suggestions">
      <h3>Session suggestions</h3>
      {body}
    </section>
  );
}

export { DISMISSED_KEY_PREFIX, SessionSuggestionsCard };
