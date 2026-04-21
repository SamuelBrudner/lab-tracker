import * as React from "react";

import { downloadProtectedResource } from "../../shared/api.js";
import { QUESTION_TYPES } from "../../shared/constants.js";
import {
  buildHighlightedSnippet,
  formatBytes,
  formatConfidence,
  formatDate,
} from "../../shared/formatters.js";
import { AppLink } from "../../shared/routing.jsx";

const { useMemo } = React;

function QuestionExtractionInboxPanel({
  canWrite,
  busy,
  token,
  selectedProjectId,
  notes,
  questions,
  navigate,
  selectedNoteId,
  onSelectedNoteIdChange,
  note,
  noteRaw,
  noteRawError = "",
  candidates,
  onExtractCandidates,
  onUpdateCandidate,
  onToggleCandidateSelected,
  onSelectAllPending,
  onClearSelection,
  onRejectSelected,
  onStageSelected,
  onFlash,
}) {
  const sortedNotes = useMemo(() => {
    return [...notes].sort((a, b) => {
      const aTime = new Date(a.created_at || 0).getTime();
      const bTime = new Date(b.created_at || 0).getTime();
      return bTime - aTime;
    });
  }, [notes]);

  const noteText = note ? note.transcribed_text || note.raw_content || "" : "";
  const rawContentType = noteRaw?.content_type || "";
  const hasRawBytes = Boolean(noteRaw?.content_base64);
  const rawIsImage = rawContentType.startsWith("image/");
  const rawImageSrc =
    rawIsImage && hasRawBytes ? `data:${rawContentType};base64,${noteRaw.content_base64}` : "";

  async function handleDownloadRaw() {
    if (!token || !note || !noteRaw) {
      return;
    }
    try {
      await downloadProtectedResource({
        path: `/notes/${note.note_id}/raw`,
        token,
        filename: noteRaw.filename,
      });
    } catch (err) {
      if (typeof onFlash === "function") {
        onFlash("", err.message || "Failed to download note.");
      }
    }
  }

  const totalCount = candidates.length;
  const pendingCount = candidates.filter((item) => item.status === "pending").length;
  const selectedPendingCount = candidates.filter(
    (item) => item.selected && item.status === "pending"
  ).length;
  const stagedCount = candidates.filter((item) => item.status === "staged").length;
  const rejectedCount = candidates.filter((item) => item.status === "rejected").length;

  return (
    <article className="card span-12">
      <div className="item-head">
        <h2>Question Staging Inbox (Extracted)</h2>
        {busy ? <span className="pill">Working...</span> : null}
      </div>
      <p className="subtle">
        Extract question candidates from a note, review suggested metadata, and stage accepted
        questions for later commit.
      </p>

      <form className="form" onSubmit={onExtractCandidates}>
        <label>
          Source note
          <select
            value={selectedNoteId}
            onChange={onSelectedNoteIdChange}
            disabled={!token || !selectedProjectId || notes.length === 0}
          >
            <option value="">Select a note</option>
            {sortedNotes.map((item) => {
              const preview = item.transcribed_text || item.raw_content || "(binary upload)";
              const label = `${formatDate(item.created_at)} · ${preview.slice(0, 80)}`;
              return (
                <option key={item.note_id} value={item.note_id}>
                  {label}
                </option>
              );
            })}
          </select>
        </label>

        <div className="inline">
          <button
            className="btn-primary"
            disabled={!canWrite || !token || !selectedProjectId || !selectedNoteId || busy}
          >
            Extract candidates
          </button>
          <button
            type="button"
            className="btn-secondary"
            disabled={busy || (!selectedNoteId && candidates.length === 0 && !note)}
            onClick={() => onSelectedNoteIdChange({ target: { value: "" } })}
          >
            Clear
          </button>
          {note ? (
            <button
              type="button"
              className="btn-secondary"
              onClick={() => navigate(`/app/notes/${note.note_id}`)}
            >
              Open note detail
            </button>
          ) : null}
        </div>
      </form>

      {!canWrite ? (
        <p className="warn">
          Extraction review requires write access. Ask an admin to provision an editor or admin
          account.
        </p>
      ) : null}

      {note ? (
        <div className="review-layout">
          <section className="review-pane">
            <div className="item-head">
              <h3>Source</h3>
              <div className="inline">
                <span className="pill">{note.status}</span>
                <span className="subtle">{formatDate(note.created_at)}</span>
              </div>
            </div>

            {noteRawError ? <p className="warn">Raw preview unavailable: {noteRawError}</p> : null}

            {hasRawBytes ? (
              rawIsImage ? (
                <img className="note-image" src={rawImageSrc} alt="Source note" />
              ) : (
                <div className="item">
                  <div className="item-head">
                    <strong>Raw asset</strong>
                    <span className="subtle">{rawContentType || "unknown type"}</span>
                  </div>
                  <p className="mono">{noteRaw.filename}</p>
                  <p className="mono">{formatBytes(noteRaw.size_bytes)}</p>
                  <button type="button" className="link" onClick={handleDownloadRaw}>
                    Download
                  </button>
                </div>
              )
            ) : null}

            <div className="stack">
              <div>
                <div className="subtle">Transcribed / raw text</div>
                {noteText ? (
                  <pre className="note-text">{noteText}</pre>
                ) : (
                  <p className="subtle">(No note text available for highlighting.)</p>
                )}
              </div>
              <div className="stack">
                <div className="subtle">Note ID</div>
                <div className="mono">{note.note_id}</div>
              </div>
            </div>
          </section>

          <section className="review-pane">
            <div className="item-head">
              <h3>Candidates</h3>
              <div className="inline">
                <span className="pill" title="total candidates">
                  {totalCount}
                </span>
                <span className="pill" title="pending">
                  pending: {pendingCount}
                </span>
                <span className="pill" title="staged">
                  staged: {stagedCount}
                </span>
                <span className="pill" title="rejected">
                  rejected: {rejectedCount}
                </span>
              </div>
            </div>

            {candidates.length === 0 ? (
              <p className="subtle">
                No candidates loaded. Pick a note and click &quot;Extract candidates&quot;.
              </p>
            ) : (
              <>
                <div className="inline">
                  <button
                    type="button"
                    className="btn-secondary"
                    disabled={busy || candidates.length === 0}
                    onClick={onSelectAllPending}
                  >
                    Select pending
                  </button>
                  <button
                    type="button"
                    className="btn-secondary"
                    disabled={busy || candidates.length === 0}
                    onClick={onClearSelection}
                  >
                    Clear selection
                  </button>
                  <button
                    type="button"
                    className="btn-danger"
                    disabled={!canWrite || busy || selectedPendingCount === 0}
                    onClick={() => onRejectSelected()}
                  >
                    Reject selected
                  </button>
                  <button
                    type="button"
                    className="btn-primary"
                    disabled={!canWrite || busy || selectedPendingCount === 0 || !selectedProjectId}
                    onClick={() => onStageSelected()}
                  >
                    Stage selected ({selectedPendingCount})
                  </button>
                </div>

                <div className="stack">
                  {candidates.map((candidate) => {
                    const snippet = noteText ? buildHighlightedSnippet(noteText, candidate.text) : null;
                    const itemClass = [
                      "item",
                      candidate.status === "rejected" ? "item-rejected" : "",
                      candidate.status === "staged" ? "item-staged" : "",
                      candidate.status === "error" ? "item-error" : "",
                    ]
                      .filter(Boolean)
                      .join(" ");

                    return (
                      <article className={itemClass} key={candidate.local_id}>
                        <div className="item-head">
                          <div className="inline">
                            <input
                              type="checkbox"
                              checked={candidate.selected}
                              disabled={candidate.status !== "pending" || busy}
                              onChange={() => onToggleCandidateSelected(candidate.local_id)}
                              aria-label="Select candidate"
                            />
                            <strong>Candidate</strong>
                            <span className="pill">{formatConfidence(candidate.confidence)}</span>
                            <span className="pill">{candidate.question_type}</span>
                            <span className="pill">{candidate.status}</span>
                          </div>
                          {candidate.staged_question_id ? (
                            <AppLink
                              to={`/app/questions/${candidate.staged_question_id}`}
                              navigate={navigate}
                              className="link"
                            >
                              Open
                            </AppLink>
                          ) : null}
                        </div>

                        {snippet ? (
                          <p className="source-snippet">
                            {snippet.prefix}
                            <mark className="highlight">{snippet.match}</mark>
                            {snippet.suffix}
                          </p>
                        ) : noteText ? (
                          <p className="subtle">Source match not found in note text.</p>
                        ) : null}

                        <div className="form">
                          <label>
                            Question text
                            <textarea
                              value={candidate.text}
                              disabled={candidate.status !== "pending" || !canWrite || busy}
                              onChange={(event) =>
                                onUpdateCandidate(candidate.local_id, { text: event.target.value })
                              }
                            />
                          </label>

                          <div className="review-fields">
                            <label>
                              Type
                              <select
                                value={candidate.question_type}
                                disabled={candidate.status !== "pending" || !canWrite || busy}
                                onChange={(event) =>
                                  onUpdateCandidate(candidate.local_id, {
                                    question_type: event.target.value,
                                  })
                                }
                              >
                                {QUESTION_TYPES.map((typeValue) => (
                                  <option value={typeValue} key={typeValue}>
                                    {typeValue}
                                  </option>
                                ))}
                              </select>
                            </label>

                            <label>
                              Hypothesis (optional)
                              <input
                                value={candidate.hypothesis}
                                disabled={candidate.status !== "pending" || !canWrite || busy}
                                onChange={(event) =>
                                  onUpdateCandidate(candidate.local_id, {
                                    hypothesis: event.target.value,
                                  })
                                }
                              />
                            </label>
                          </div>

                          <label>
                            Parent questions (optional)
                            <select
                              multiple
                              value={candidate.parent_question_ids}
                              disabled={candidate.status !== "pending" || !canWrite || busy}
                              onChange={(event) => {
                                const values = Array.from(event.target.selectedOptions).map(
                                  (option) => option.value
                                );
                                onUpdateCandidate(candidate.local_id, {
                                  parent_question_ids: values,
                                });
                              }}
                            >
                              {questions.map((question) => (
                                <option value={question.question_id} key={question.question_id}>
                                  {question.text.slice(0, 80)}
                                </option>
                              ))}
                            </select>
                          </label>

                          {candidate.error ? (
                            <p className="flash error">{candidate.error}</p>
                          ) : null}

                          <div className="inline">
                            <button
                              type="button"
                              className="btn-secondary"
                              disabled={candidate.status !== "pending" || !canWrite || busy}
                              onClick={() => {
                                onRejectSelected([candidate.local_id]);
                              }}
                            >
                              Reject
                            </button>
                            <button
                              type="button"
                              className="btn-primary"
                              disabled={candidate.status !== "pending" || !canWrite || busy}
                              onClick={() => onStageSelected([candidate.local_id])}
                            >
                              Stage
                            </button>
                          </div>
                        </div>
                      </article>
                    );
                  })}
                </div>
              </>
            )}
          </section>
        </div>
      ) : null}
    </article>
  );
}

export { QuestionExtractionInboxPanel };
