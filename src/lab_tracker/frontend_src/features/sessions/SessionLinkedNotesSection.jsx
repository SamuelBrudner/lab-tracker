import * as React from "react";

import { formatDate } from "../../shared/formatters.js";
import { AppLink } from "../../shared/routing.jsx";

function SessionLinkedNotesSection({ noteState, navigate }) {
  return (
    <div className="stack">
      <div className="item-head">
        <h3>Linked Notes</h3>
        <span className="pill">{noteState.items.length}</span>
      </div>
      {noteState.loading ? <p className="subtle">Loading linked notes...</p> : null}
      {noteState.error ? <p className="flash error">{noteState.error}</p> : null}
      {noteState.items.length === 0 && !noteState.loading ? (
        <p className="subtle">(no linked notes)</p>
      ) : (
        <div className="stack">
          {noteState.items.map((note) => {
            const preview = note.transcribed_text || note.raw_content || "(binary upload)";
            return (
              <div className="item" key={note.note_id}>
                <div className="item-head">
                  <AppLink to={`/app/notes/${note.note_id}`} navigate={navigate} className="link">
                    <strong>{preview.slice(0, 120)}</strong>
                  </AppLink>
                  <span className="pill">{note.status}</span>
                </div>
                <p className="mono">{note.note_id}</p>
                <p className="subtle">{formatDate(note.created_at)}</p>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

export { SessionLinkedNotesSection };
