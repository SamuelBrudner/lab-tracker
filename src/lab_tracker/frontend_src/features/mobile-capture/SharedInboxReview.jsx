import * as React from "react";

import { formatDate } from "../../shared/formatters.js";

function shareHeadline(share) {
  if (share.file) {
    return share.filename || "Shared file";
  }
  return share.title || share.text || share.url || "Shared item";
}

function shareDetails(share) {
  const headline = shareHeadline(share);
  return [share.title, share.text, share.url]
    .map((value) => String(value || "").trim())
    .filter((value) => value && value !== headline);
}

// Explicit review step for OS share-sheet items parked by the service worker.
// Any website can POST to the share target, so nothing is imported until the
// user sees what arrived and which project it will land in, and confirms.
function SharedInboxReview({
  shares,
  projects,
  selectedProjectId,
  canWrite,
  busy,
  onImport,
  onDiscard,
}) {
  if (shares.length === 0) {
    return null;
  }
  const targetProject = projects.find((project) => project.project_id === selectedProjectId);
  const count = shares.length;
  const noun = count === 1 ? "shared item" : "shared items";
  return (
    <section aria-labelledby="shared-inbox-review-heading" className="capture-pending">
      <h3 id="shared-inbox-review-heading">Review shared items</h3>
      <p className="subtle">
        Nothing has been saved yet. Import only items you shared yourself; discard anything you
        don&apos;t recognise.
      </p>
      <ul className="stack">
        {shares.map((share) => (
          <li className="review-queue-item" key={share.id}>
            <span className="pill">{share.file ? share.contentType || "file" : "text"}</span>
            <strong>{shareHeadline(share)}</strong>
            {shareDetails(share).map((detail, index) => (
              <span className="source-snippet" key={index}>
                {detail}
              </span>
            ))}
            <span className="subtle">{formatDate(share.receivedAt)}</span>
          </li>
        ))}
      </ul>
      <p>
        Import into project:{" "}
        <strong>{targetProject ? targetProject.name : "No project selected"}</strong>
      </p>
      {!canWrite ? (
        <p className="subtle">You need write access to this project to import shared items.</p>
      ) : null}
      <div className="inline">
        <button
          className="btn-primary"
          disabled={!canWrite || !targetProject || busy}
          onClick={onImport}
          type="button"
        >
          {targetProject ? `Import ${count} ${noun} into ${targetProject.name}` : "Import"}
        </button>
        <button className="btn-secondary" disabled={busy} onClick={onDiscard} type="button">
          Discard
        </button>
      </div>
    </section>
  );
}

export { SharedInboxReview };
