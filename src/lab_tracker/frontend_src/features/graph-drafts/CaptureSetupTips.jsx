import * as React from "react";

import {
  appPathLabel,
  captureSetupTips,
  MAX_NOTE_LINKS,
  MODEL_EXPLANATION,
} from "./capture-setup.js";

// "Help future captures": the capture-setup tips the drafter picked for this
// batch, with the setup steps Lab Tracker attached. Read-only advice for the
// person: tips are not proposals, so nothing here is accepted, rejected,
// deferred, counted, or committed, and the review keyboard never lands here.
function CaptureSetupTip({ tip, navigate }) {
  return (
    <li className="stack">
      <div>
        <strong>{tip.title}</strong>
        {tip.sessionLabel ? <span className="subtle"> · {tip.sessionLabel}</span> : null}
      </div>
      {tip.explanation ? (
        <p>
          {tip.explanationSource === MODEL_EXPLANATION ? "Drafter: " : "Lab Tracker check: "}
          {tip.explanation}
        </p>
      ) : null}
      {tip.noteIds.length > 0 ? (
        <div className="inline">
          <span className="subtle">
            Based on {tip.noteIds.length} {tip.noteIds.length === 1 ? "capture" : "captures"}:
          </span>
          {tip.noteIds.slice(0, MAX_NOTE_LINKS).map((noteId, index) => (
            <button
              key={noteId}
              type="button"
              className="btn-link"
              onClick={() => navigate(`/app/notes/${noteId}`)}
            >
              Capture {index + 1}
            </button>
          ))}
        </div>
      ) : null}
      <ol>
        {tip.steps.map((step, index) => (
          <li key={index}>{step}</li>
        ))}
      </ol>
      {tip.command ? <code className="mono">{tip.command}</code> : null}
      {tip.appPath ? (
        <div className="inline">
          <button type="button" className="btn-secondary" onClick={() => navigate(tip.appPath)}>
            {appPathLabel(tip.appPath)}
          </button>
        </div>
      ) : null}
      {tip.doc ? (
        <div className="subtle">
          Guide: <span className="mono">{tip.doc}</span>
        </div>
      ) : null}
    </li>
  );
}

function CaptureSetupTips({ changeSet, navigate }) {
  const tips = React.useMemo(() => captureSetupTips(changeSet), [changeSet]);
  if (tips.length === 0) {
    return null;
  }
  return (
    <section className="review-unsure stack" aria-labelledby="capture-setup-tips-title">
      <h3 id="capture-setup-tips-title">Help future captures</h3>
      <p className="subtle">
        Setup that would have given the drafter what these captures were missing. Nothing here
        changes this draft.
      </p>
      <ul className="compact-list">
        {tips.map((tip) => (
          <CaptureSetupTip key={tip.id} tip={tip} navigate={navigate} />
        ))}
      </ul>
    </section>
  );
}

export { CaptureSetupTips };
