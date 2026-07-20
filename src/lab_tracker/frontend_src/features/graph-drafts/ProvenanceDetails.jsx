import * as React from "react";

import { formatDate } from "../../shared/formatters.js";
import { contextCountLabel, statusClass } from "./format.js";

// Presentational, read-only "Details & provenance" disclosure for a change set:
// status/model pills, source notes and artifacts, lifecycle timestamps, and the
// context-packet summary.
function ProvenanceDetails({ changeSet }) {
  return (
    <details className="context-details review-meta">
      <summary>Details &amp; provenance</summary>
      <div className="stack">
        <div className="inline">
          <span className={statusClass(changeSet.status)}>{changeSet.status}</span>
          <span className="pill">{changeSet.draft_mode || "graph_context"}</span>
          <span className="pill">{changeSet.model}</span>
          <span className="pill">{changeSet.provider}</span>
        </div>
        <div>
          <div className="subtle">Source note</div>
          <div className="mono">{changeSet.source_note_id}</div>
        </div>
        {(changeSet.source_note_ids || []).length > 1 ? (
          <div>
            <div className="subtle">Notes in this review</div>
            <div className="inline">
              {(changeSet.source_note_ids || []).map((noteId) => (
                <span className="pill mono" key={noteId}>
                  {noteId}
                </span>
              ))}
            </div>
          </div>
        ) : null}
        {(changeSet.context_packet?.source_artifacts || []).length > 0 ? (
          <div>
            <div className="subtle">Source artifacts and transcripts</div>
            <div className="stack">
              {changeSet.context_packet.source_artifacts.map((artifact) => (
                <article className="item" key={artifact.note_id || artifact.artifact_id}>
                  <div className="inline">
                    <span className="pill">{artifact.type || "source"}</span>
                    {artifact.content_type ? (
                      <span className="pill">{artifact.content_type}</span>
                    ) : null}
                  </div>
                  <p className="mono">{artifact.filename || artifact.note_id}</p>
                  {artifact.transcript_text ? (
                    <p className="source-snippet">{artifact.transcript_text}</p>
                  ) : null}
                </article>
              ))}
            </div>
          </div>
        ) : null}
        <div>
          <div className="subtle">Created</div>
          <div className="mono">
            {formatDate(changeSet.created_at)}
            {changeSet.created_by_username ? ` by ${changeSet.created_by_username}` : ""}
          </div>
        </div>
        {changeSet.submitted_at ? (
          <div>
            <div className="subtle">Submitted</div>
            <div className="mono">
              {formatDate(changeSet.submitted_at)}
              {changeSet.submitted_by_username ? ` by ${changeSet.submitted_by_username}` : ""}
            </div>
          </div>
        ) : null}
        {changeSet.reviewed_at ? (
          <div>
            <div className="subtle">Reviewed</div>
            <div className="mono">
              {formatDate(changeSet.reviewed_at)}
              {changeSet.reviewed_by_username ? ` by ${changeSet.reviewed_by_username}` : ""}
            </div>
            {changeSet.review_note ? <p>{changeSet.review_note}</p> : null}
          </div>
        ) : null}
        {changeSet.committed_at ? (
          <div>
            <div className="subtle">Committed</div>
            <div className="mono">
              {formatDate(changeSet.committed_at)}
              {changeSet.committed_by_username ? ` by ${changeSet.committed_by_username}` : ""}
            </div>
          </div>
        ) : null}
        {changeSet.context_packet?.context_summary ? (
          <div>
            <div className="subtle">Context summary</div>
            <div className="inline">
              <span className="pill">
                ~{changeSet.context_packet.context_summary.approximate_size_bytes || 0} bytes
              </span>
              {Object.entries(changeSet.context_packet.context_summary.counts || {}).map(
                ([key, value]) => (
                  <span className="pill" key={key}>
                    {contextCountLabel(key)}: {value}
                  </span>
                )
              )}
            </div>
            {Object.keys(changeSet.context_packet.context_summary.source_artifact_counts || {})
              .length > 0 ? (
              <p className="subtle">
                Source artifacts:{" "}
                {Object.entries(changeSet.context_packet.context_summary.source_artifact_counts)
                  .map(([key, value]) => `${key} ${value}`)
                  .join(", ")}
              </p>
            ) : null}
            {(changeSet.context_packet.context_summary.warnings || []).length > 0 ? (
              <ul className="compact-list">
                {changeSet.context_packet.context_summary.warnings.map((warning) => (
                  <li key={warning}>{warning}</li>
                ))}
              </ul>
            ) : null}
          </div>
        ) : null}
        {changeSet.context_packet ? (
          <details className="context-details">
            <summary>Graph context used</summary>
            <pre className="manifest-preview">
              {JSON.stringify(changeSet.context_packet, null, 2)}
            </pre>
          </details>
        ) : null}
      </div>
    </details>
  );
}

export { ProvenanceDetails };
