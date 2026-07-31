import * as React from "react";

import {
  operationIntent,
  operationNarrativeAction,
  operationProposalText,
  sourceRefText,
  statusClass,
} from "./format.js";

function SummaryParagraphs({ summary }) {
  if (!summary) {
    return null;
  }
  return (
    <div className="review-narrative-summary">
      {String(summary)
        .split(/\n{2,}/)
        .map((paragraph) => paragraph.trim())
        .filter(Boolean)
        .map((paragraph, index) => (
          <p key={index}>{paragraph}</p>
        ))}
    </div>
  );
}

function ProposalCitation({
  operation,
  index,
  payloads,
  reviewNote,
  canEditDraft,
  pending,
  isOpen,
  isPinned,
  onOpen,
  onClose,
  onTogglePinned,
  onUpdateReviewNote,
  onSaveOperation,
}) {
  const panelId = `proposal-citation-${operation.operation_id}`;
  const proposedText = operationProposalText(operation, payloads);
  const editNumber = index + 1;

  return (
    <span
      className="proposal-citation-wrap"
      onBlur={(event) => {
        if (!event.currentTarget.contains(event.relatedTarget) && !isPinned) {
          onClose();
        }
      }}
      onFocus={() => onOpen()}
      onKeyDown={(event) => {
        if (event.key === "Escape") {
          event.stopPropagation();
          onClose({ unpin: true });
        }
      }}
      onMouseEnter={() => onOpen()}
      onMouseLeave={() => {
        if (!isPinned) {
          onClose();
        }
      }}
    >
      <button
        type="button"
        className="proposal-citation"
        aria-controls={panelId}
        aria-expanded={isOpen}
        aria-label={`Proposed edit ${editNumber}: ${operationIntent(operation)}`}
        onClick={onTogglePinned}
      >
        [{editNumber}]
      </button>
      {isOpen ? (
        <span
          className="proposal-citation-card"
          id={panelId}
          role="group"
          aria-label={`Proposed edit ${editNumber} details`}
        >
          <span className="proposal-citation-heading">
            <strong>Proposed edit {editNumber}</strong>
            <span className={statusClass(operation.status)}>{operation.status}</span>
          </span>
          <span className="proposal-citation-intent">{operationIntent(operation)}</span>
          <span className="proposal-citation-text">{proposedText}</span>
          {operation.rationale ? (
            <span className="proposal-citation-rationale">
              <span className="subtle">Model inference</span> {operation.rationale}
              {operation.confidence !== null && operation.confidence !== undefined
                ? ` · ${Math.round(operation.confidence * 100)}% confident`
                : ""}
            </span>
          ) : null}
          {(operation.source_refs || []).length > 0 ? (
            <span className="proposal-citation-evidence">
              <span className="subtle">Source evidence</span>
              {(operation.source_refs || []).map((ref, refIndex) => (
                <span
                  className="source-snippet"
                  key={`${operation.operation_id}-${refIndex}`}
                >
                  {sourceRefText(ref)}
                </span>
              ))}
            </span>
          ) : null}
          <label className="proposal-citation-note">
            Note for proposed edit {editNumber}
            <textarea
              value={reviewNote || ""}
              disabled={!canEditDraft || Boolean(pending)}
              onChange={(event) =>
                onUpdateReviewNote(operation.operation_id, event.target.value)
              }
            />
          </label>
          <span className="proposal-citation-actions">
            <button
              type="button"
              className="btn-primary"
              disabled={!canEditDraft || Boolean(pending)}
              onClick={() => onSaveOperation(operation, "accepted")}
            >
              Accept edit
            </button>
            <button
              type="button"
              className="btn-danger"
              disabled={!canEditDraft || Boolean(pending)}
              onClick={() => onSaveOperation(operation, "rejected")}
            >
              Reject edit
            </button>
            <button
              type="button"
              className="btn-secondary"
              disabled={!canEditDraft || Boolean(pending)}
              onClick={() => onSaveOperation(operation)}
            >
              Save note
            </button>
          </span>
          <button
            type="button"
            className="btn-link proposal-citation-close"
            onClick={() => onClose({ unpin: true })}
          >
            Close details
          </button>
        </span>
      ) : null}
    </span>
  );
}

function NarrativeReview({
  changeSet,
  payloads,
  operationReviewNotes,
  canEditDraft,
  pendingCommands,
  onUpdateOperationReviewNote,
  onSaveOperation,
}) {
  const [openCitationId, setOpenCitationId] = React.useState("");
  const [pinnedCitationId, setPinnedCitationId] = React.useState("");
  const operations = React.useMemo(() => changeSet.operations || [], [changeSet.operations]);

  React.useEffect(() => {
    const availableIds = new Set(operations.map((operation) => operation.operation_id));
    if (openCitationId && !availableIds.has(openCitationId)) {
      setOpenCitationId("");
    }
    if (pinnedCitationId && !availableIds.has(pinnedCitationId)) {
      setPinnedCitationId("");
    }
  }, [openCitationId, operations, pinnedCitationId]);

  return (
    <section className="review-narrative" aria-label="Narrative review">
      <p className="review-narrative-intro">
        Read this as an account of the work. Each numbered citation is a specific proposed
        graph edit. Hover, focus, or click a citation to inspect it, make a decision, or leave
        a note.
      </p>
      <SummaryParagraphs summary={changeSet.summary} />
      <div className="review-narrative-prose">
        {operations.length === 0 ? (
          <p>No graph edits were proposed in this draft.</p>
        ) : (
          operations.map((operation, index) => {
            const operationId = operation.operation_id;
            const proposedText =
              operationProposalText(operation, payloads) || operationIntent(operation);
            const isOpen = openCitationId === operationId;
            const isPinned = pinnedCitationId === operationId;
            return (
              <p key={operationId}>
                {index === 0 ? "The draft proposes to " : "It also proposes to "}
                {operationNarrativeAction(operation)}
                {": "}
                <span className="review-narrative-proposal">&ldquo;{proposedText}&rdquo;</span>
                {operation.rationale ? (
                  <>
                    {" "}
                    The model connects this to the work because {operation.rationale}
                  </>
                ) : null}{" "}
                <ProposalCitation
                  operation={operation}
                  index={index}
                  payloads={payloads}
                  reviewNote={operationReviewNotes[operationId]}
                  canEditDraft={canEditDraft}
                  pending={pendingCommands[`op:${operationId}`]}
                  isOpen={isOpen}
                  isPinned={isPinned}
                  onOpen={() => setOpenCitationId(operationId)}
                  onClose={({ unpin = false } = {}) => {
                    setOpenCitationId("");
                    if (unpin) {
                      setPinnedCitationId("");
                    }
                  }}
                  onTogglePinned={() => {
                    if (isPinned) {
                      setPinnedCitationId("");
                      setOpenCitationId("");
                    } else {
                      setPinnedCitationId(operationId);
                      setOpenCitationId(operationId);
                    }
                  }}
                  onUpdateReviewNote={onUpdateOperationReviewNote}
                  onSaveOperation={onSaveOperation}
                />
              </p>
            );
          })
        )}
      </div>
    </section>
  );
}

export { NarrativeReview, ProposalCitation };
