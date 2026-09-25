import * as React from "react";

// Reasons a capture can be set aside, in the order the review offers them.
// The values are the server's NoteArchiveReason enum.
const ARCHIVE_REASONS = Object.freeze([
  { value: "reviewed_not_relevant", label: "Reviewed, not relevant" },
  { value: "superseded", label: "Superseded by a later capture" },
  { value: "archived_unreviewed", label: "Set aside without review" },
]);
const DEFAULT_ARCHIVE_REASON = "reviewed_not_relevant";

// Per source capture: a reason and a "Set aside" action. Purely presentational;
// the workflow controller owns the request and the refresh.
function SourceNoteActions({ noteIds, canEditDraft, pendingCommands, onArchive }) {
  const [reasons, setReasons] = React.useState({});
  if (!noteIds || noteIds.length === 0) {
    return null;
  }
  return (
    <section className="review-source-notes" aria-label="Source captures">
      <div className="subtle">Source captures</div>
      <ul className="compact-list">
        {noteIds.map((noteId) => {
          const reason = reasons[noteId] || DEFAULT_ARCHIVE_REASON;
          const pending = Boolean(pendingCommands[`archive:${noteId}`]);
          return (
            <li className="review-source-note" key={noteId}>
              <span className="mono">{noteId}</span>
              <label>
                Reason
                <select
                  aria-label={`Set-aside reason for ${noteId}`}
                  disabled={!canEditDraft || pending}
                  onChange={(event) =>
                    setReasons((current) => ({ ...current, [noteId]: event.target.value }))
                  }
                  value={reason}
                >
                  {ARCHIVE_REASONS.map((option) => (
                    <option key={option.value} value={option.value}>
                      {option.label}
                    </option>
                  ))}
                </select>
              </label>
              <button
                type="button"
                className="btn-secondary"
                disabled={!canEditDraft || pending}
                onClick={() => onArchive(noteId, reason)}
                title="Archive this capture with the chosen reason; the proposals stay reviewable"
              >
                Set aside
              </button>
            </li>
          );
        })}
      </ul>
    </section>
  );
}

export { ARCHIVE_REASONS, DEFAULT_ARCHIVE_REASON, SourceNoteActions };
