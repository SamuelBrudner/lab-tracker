import * as React from "react";

import { useGraphDraftWorkflow } from "../hooks/useGraphDraftWorkflow.js";
import { useReviewDictation } from "../hooks/useReviewDictation.js";
import { useSourceArtifactPreviews } from "../hooks/useSourceArtifactPreviews.js";
import { apiListRequest, buildApiPath } from "../shared/api.js";
import { AudioReviewConsole } from "./graph-drafts/AudioReviewConsole.jsx";
import { NarrativeReview } from "./graph-drafts/NarrativeReview.jsx";
import { OperationRow } from "./graph-drafts/OperationRow.jsx";
import { ProvenanceDetails } from "./graph-drafts/ProvenanceDetails.jsx";
import { SourceArtifactEvidence } from "./graph-drafts/SourceArtifactEvidence.jsx";
import { decisionCounts, spokenReviewScript } from "./graph-drafts/format.js";
import { buildSourceArtifactReview } from "./graph-drafts/source-artifacts.js";

const DECISION_KEYS = { a: "accepted", d: "proposed", r: "rejected" };
const REVIEWABLE_STATUSES = new Set(["ready", "changes_requested"]);

function isTypingTarget(target) {
  const tag = target?.tagName;
  return (
    tag === "INPUT" ||
    tag === "TEXTAREA" ||
    tag === "SELECT" ||
    Boolean(target?.isContentEditable)
  );
}

// After a commit: hand the reviewer straight to the next review that is
// waiting for them, or back to the queue when there is none.
function AfterCommitActions({ token, currentChangeSetId, navigate, backPath }) {
  const [nextId, setNextId] = React.useState(null);
  React.useEffect(() => {
    let canceled = false;
    apiListRequest(buildApiPath("/batches", { limit: 5, mine: true }), { token })
      .then(({ data }) => {
        if (canceled) {
          return;
        }
        const next = (data || []).find(
          (batch) =>
            batch.change_set_id !== currentChangeSetId && REVIEWABLE_STATUSES.has(batch.status)
        );
        setNextId(next?.change_set_id || "");
      })
      .catch(() => {
        if (!canceled) {
          setNextId("");
        }
      });
    return () => {
      canceled = true;
    };
  }, [currentChangeSetId, token]);

  return (
    <div className="review-committed" role="status">
      <div>
        <strong>Committed.</strong>{" "}
        <span className="subtle">
          {nextId === null
            ? "Checking for your next review…"
            : nextId
              ? "Another review is waiting for you."
              : "Nothing else is waiting for your review."}
        </span>
      </div>
      <div className="inline">
        {nextId ? (
          <button
            type="button"
            className="btn-primary"
            onClick={() => navigate(`/app/batches/${nextId}`)}
          >
            Next review
          </button>
        ) : null}
        <button type="button" className="btn-secondary" onClick={() => navigate(backPath)}>
          Back to queue
        </button>
      </div>
    </div>
  );
}

function GraphDraftDetailCard({
  token,
  changeSetId,
  navigate,
  canWrite,
  canManageGraph = false,
  user = null,
  setBusy,
  setFlash,
  backPath = "/app",
  allowBulkAccept = true,
}) {
  const [reviewView, setReviewView] = React.useState("proposals");
  const workflow = useGraphDraftWorkflow({
    token,
    changeSetId,
    canWrite,
    canManageGraph,
    user,
    setBusy,
    setFlash,
  });
  const dictation = useReviewDictation({
    changeSetId,
    spokenReview: workflow.spokenReview,
    canEditDraft: workflow.canEditDraft,
    setFlash,
  });

  const {
    changeSet,
    payloads,
    operationReviewNotes,
    loading,
    error,
    accessError,
    commitMessage,
    setCommitMessage,
    suggestedCommitMessage,
    reviewNote,
    setReviewNote,
    pendingCommands,
    acceptedCount,
    undoableOperationIds,
    canEditDraft,
    canReviseDraft,
    supportsAiRevision,
    canSubmitDraft,
    canReviewDraft,
    canCommitDraft,
  } = workflow;
  const sourceReview = React.useMemo(
    () => buildSourceArtifactReview(changeSet),
    [changeSet]
  );
  const operations = React.useMemo(() => changeSet?.operations || [], [changeSet]);
  const counts = React.useMemo(() => decisionCounts(changeSet), [changeSet]);

  // Keyboard review loop over the proposal cards: j/k (or the arrows) move
  // the focused card, a / r / d decide it and move on to the next undecided
  // one. Shortcuts stay out of the way while a field is being typed in.
  const [focusedOperationId, setFocusedOperationId] = React.useState("");
  const rowRefs = React.useRef({});
  const keyHandlerRef = React.useRef(null);

  function focusOperationAt(index) {
    const operation = operations[index];
    if (!operation) {
      return;
    }
    setFocusedOperationId(operation.operation_id);
    const node = rowRefs.current[operation.operation_id];
    if (node) {
      node.focus?.({ preventScroll: true });
      node.scrollIntoView?.({ block: "nearest" });
    }
  }

  function nextUndecidedIndex(from) {
    for (let index = from + 1; index < operations.length; index += 1) {
      if (operations[index].status === "proposed") {
        return index;
      }
    }
    for (let index = 0; index < from; index += 1) {
      if (operations[index].status === "proposed") {
        return index;
      }
    }
    return -1;
  }

  async function handleReviewKeyDown(event) {
    if (
      reviewView !== "proposals" ||
      !changeSet ||
      operations.length === 0 ||
      event.defaultPrevented ||
      event.altKey ||
      event.ctrlKey ||
      event.metaKey ||
      isTypingTarget(event.target)
    ) {
      return;
    }
    const current = operations.findIndex(
      (operation) => operation.operation_id === focusedOperationId
    );
    if (event.key === "j" || event.key === "ArrowDown") {
      event.preventDefault();
      focusOperationAt(current < 0 ? 0 : Math.min(current + 1, operations.length - 1));
      return;
    }
    if (event.key === "k" || event.key === "ArrowUp") {
      event.preventDefault();
      focusOperationAt(current < 0 ? 0 : Math.max(current - 1, 0));
      return;
    }
    const decision = DECISION_KEYS[event.key];
    if (!decision || !canEditDraft || current < 0) {
      return;
    }
    event.preventDefault();
    const operation = operations[current];
    if (pendingCommands[`op:${operation.operation_id}`]) {
      return;
    }
    const saved = await workflow.saveOperation(operation, decision);
    if (saved) {
      const next = nextUndecidedIndex(current);
      if (next >= 0) {
        focusOperationAt(next);
      }
    }
  }

  React.useEffect(() => {
    keyHandlerRef.current = handleReviewKeyDown;
  });
  React.useEffect(() => {
    const listener = (event) => keyHandlerRef.current?.(event);
    document.addEventListener("keydown", listener);
    return () => document.removeEventListener("keydown", listener);
  }, []);
  const sourcePreviews = useSourceArtifactPreviews(sourceReview.artifactsToLoad, token);
  const reviewAttachmentEvidence = changeSet?.context_packet?.review_attachment_evidence;
  const effectiveAllowBulkAccept =
    allowBulkAccept && changeSet?.purpose !== "member_checkpoint_alignment";
  const reviewAttachmentMessage =
    reviewAttachmentEvidence?.status === "unavailable"
      ? reviewAttachmentEvidence.message ||
        "Reviewer attachment previews are unavailable for this revision."
      : "";

  async function handleRevise() {
    const revised = await workflow.reviseDraft({
      isRecording: dictation.isRecording,
      feedback: dictation.reviseFeedback,
      audioFile: dictation.reviseAudio?.file || null,
      attachments: dictation.reviseAttachments,
    });
    if (revised) {
      dictation.resetReviseInputs();
    }
  }

  return (
    <article className="card span-12">
      <div className="item-head">
        <div className="review-title-row">
          <button type="button" className="btn-link" onClick={() => navigate(backPath)}>
            Back
          </button>
          <h2>Review</h2>
        </div>
        {loading ? <span className="pill">Loading...</span> : null}
      </div>
      {error ? <p className="flash error">{error}</p> : null}
      {accessError ? <p className="flash error">{accessError}</p> : null}

      {changeSet ? (
        <div className="daily-review-report">
          {changeSet.summary && reviewView === "proposals" ? (
            <div className="review-summary">
              {String(changeSet.summary)
                .split(/\n{2,}/)
                .map((para) => para.trim())
                .filter(Boolean)
                .map((para, index) => (
                  <p key={index}>{para}</p>
                ))}
            </div>
          ) : null}
          <p className="review-lead subtle">
            {changeSet.source_note_count || (changeSet.source_note_ids || []).length || 1}{" "}
            {(changeSet.source_note_count || (changeSet.source_note_ids || []).length || 1) === 1
              ? "capture"
              : "captures"}{" "}
            from this review became{" "}
            {(changeSet.operations || []).length === 1
              ? "1 proposal"
              : `${(changeSet.operations || []).length} proposals`}{" "}
            for your graph. Keep what&apos;s right, then commit — nothing changes until you do.
          </p>
          <AudioReviewConsole
            speechStatus={dictation.speechStatus}
            speechSupported={dictation.speechSupported}
            recordingSupported={dictation.recordingSupported}
            isRecording={dictation.isRecording}
            canEditDraft={canReviseDraft}
            revisionSupported={supportsAiRevision}
            spokenReview={workflow.spokenReview}
            reviseAudio={dictation.reviseAudio}
            reviseFeedback={dictation.reviseFeedback}
            setReviseFeedback={dictation.setReviseFeedback}
            reviseAttachments={dictation.reviseAttachments}
            reviseInFlight={Boolean(pendingCommands.revise)}
            onToggleSpeech={dictation.toggleSpeech}
            onStopSpeech={dictation.stopSpeech}
            onToggleRecording={dictation.toggleRecording}
            onClearReviseAudio={dictation.clearReviseAudio}
            onAttachmentChange={dictation.handleAttachmentChange}
            onRemoveAttachment={dictation.removeAttachment}
            onRevise={handleRevise}
          />
          {changeSet.error_metadata?.message ? (
            <p className="flash error">{changeSet.error_metadata.message}</p>
          ) : null}

          <SourceArtifactEvidence
            artifacts={sourceReview.sharedArtifacts}
            previews={sourcePreviews}
            shared
            sharedMessage={reviewAttachmentMessage}
          />
          {reviewAttachmentMessage && sourceReview.sharedArtifacts.length === 0 ? (
            <p className="source-artifact-warning" role="status">
              {reviewAttachmentMessage}
            </p>
          ) : null}

          <div className="review-view-switch">
            <span className="subtle">Review as</span>
            <div className="review-view-toggle" role="group" aria-label="Review view">
              <button
                type="button"
                className={reviewView === "narrative" ? "active" : ""}
                aria-pressed={reviewView === "narrative"}
                onClick={() => setReviewView("narrative")}
              >
                Narrative
              </button>
              <button
                type="button"
                className={reviewView === "proposals" ? "active" : ""}
                aria-pressed={reviewView === "proposals"}
                onClick={() => setReviewView("proposals")}
              >
                Proposals
              </button>
            </div>
          </div>

          {reviewView === "narrative" ? (
            <NarrativeReview
              changeSet={changeSet}
              payloads={payloads}
              operationReviewNotes={operationReviewNotes}
              canEditDraft={canEditDraft}
              pendingCommands={pendingCommands}
              onUpdateOperationReviewNote={workflow.updateOperationReviewNote}
              onSaveOperation={workflow.saveOperation}
            />
          ) : (
            <div className="review-report">
              {canEditDraft && operations.length > 0 ? (
                <p className="review-keys subtle" aria-label="Keyboard shortcuts">
                  Keyboard: <kbd>j</kbd> / <kbd>k</kbd> next and previous proposal ·{" "}
                  <kbd>a</kbd> accept · <kbd>r</kbd> reject · <kbd>d</kbd> defer
                </p>
              ) : null}
              {operations.map((operation) => {
                const sourceMapping = sourceReview.byOperationId[operation.operation_id] || {
                  ambiguous: false,
                  artifacts: [],
                };
                return (
                  <OperationRow
                    key={operation.operation_id}
                    operation={operation}
                    changeSet={changeSet}
                    payloadText={payloads[operation.operation_id]}
                    reviewNote={operationReviewNotes[operation.operation_id]}
                    canEditDraft={canEditDraft}
                    pending={pendingCommands[`op:${operation.operation_id}`]}
                    focused={focusedOperationId === operation.operation_id}
                    onFocusRow={() => setFocusedOperationId(operation.operation_id)}
                    rowRef={(node) => {
                      rowRefs.current[operation.operation_id] = node;
                    }}
                    sourceArtifacts={sourceMapping.artifacts}
                    sourcePreviews={sourcePreviews}
                    usesSharedSourceEvidence={sourceMapping.ambiguous}
                    onPatchOperationPayload={workflow.patchOperationPayload}
                    onUpdatePayloadText={workflow.updatePayloadText}
                    onUpdateOperationReviewNote={workflow.updateOperationReviewNote}
                    onSaveOperation={workflow.saveOperation}
                  />
                );
              })}
            </div>
          )}

          {(changeSet.uncertain_fields || []).length > 0 ||
          (changeSet.clarification_requests || []).length > 0 ? (
            <div className="review-unsure">
              <div className="subtle">The model wasn&apos;t sure about</div>
              <ul className="compact-list">
                {(changeSet.clarification_requests || []).map((item) => (
                  <li key={item}>{item}</li>
                ))}
                {(changeSet.uncertain_fields || []).map((item) => (
                  <li key={item}>{item}</li>
                ))}
              </ul>
            </div>
          ) : null}

          <div className="review-actions">
            <div className="review-tally">
              <span>
                <strong>
                  {acceptedCount} of {operations.length} kept
                </strong>
                <span className="subtle">
                  {" "}
                  · {counts.rejected} rejected · {counts.proposed} undecided
                </span>
              </span>
              {effectiveAllowBulkAccept ? (
                <button
                  type="button"
                  className="btn-secondary"
                  disabled={
                    !canEditDraft ||
                    Boolean(pendingCommands.acceptAll) ||
                    Boolean(pendingCommands.undoAcceptAll)
                  }
                  onClick={workflow.acceptAll}
                >
                  Accept all
                </button>
              ) : null}
            </div>

            {effectiveAllowBulkAccept && canEditDraft && undoableOperationIds.length > 0 ? (
              <div className="inline" role="status">
                <span className="subtle">
                  {undoableOperationIds.length === 1
                    ? "1 proposal accepted as a batch."
                    : `${undoableOperationIds.length} proposals accepted as a batch.`}
                </span>
                <button
                  type="button"
                  className="btn-secondary"
                  disabled={
                    Boolean(pendingCommands.acceptAll) ||
                    Boolean(pendingCommands.undoAcceptAll)
                  }
                  onClick={workflow.undoAcceptAll}
                >
                  Undo accept all
                </button>
              </div>
            ) : null}

            <div className="inline">
              <button
                type="button"
                className="btn-secondary"
                disabled={!canSubmitDraft || Boolean(pendingCommands.submit)}
                onClick={workflow.submitDraft}
              >
                Submit for review
              </button>
            </div>

            <form className="form" onSubmit={workflow.commitDraft}>
              {canReviewDraft ? (
                <label>
                  Review note
                  <textarea value={reviewNote} onChange={(event) => setReviewNote(event.target.value)} />
                </label>
              ) : null}
              {canReviewDraft ? (
                <div className="inline">
                  <button
                    type="button"
                    className="btn-secondary"
                    disabled={Boolean(pendingCommands.review)}
                    onClick={() => workflow.reviewDraft("changes_requested")}
                  >
                    Request changes
                  </button>
                  <button
                    type="button"
                    className="btn-danger"
                    disabled={Boolean(pendingCommands.review)}
                    onClick={() => workflow.reviewDraft("rejected")}
                  >
                    Reject draft
                  </button>
                </div>
              ) : null}
              <label>
                Commit message (optional)
                <input
                  value={commitMessage}
                  onChange={(event) => setCommitMessage(event.target.value)}
                  disabled={!canCommitDraft}
                  placeholder={suggestedCommitMessage}
                />
              </label>
              <button
                className="btn-primary"
                disabled={!canCommitDraft || acceptedCount === 0 || Boolean(pendingCommands.commit)}
              >
                Commit accepted changes
              </button>
            </form>

            {changeSet.status === "committed" ? (
              <AfterCommitActions
                token={token}
                currentChangeSetId={changeSetId}
                navigate={navigate}
                backPath={backPath}
              />
            ) : null}
          </div>

          <ProvenanceDetails changeSet={changeSet} />
        </div>
      ) : null}

      <div className="inline detail-actions">
        <button type="button" className="btn-secondary" onClick={() => navigate(backPath)}>
          Back
        </button>
      </div>
    </article>
  );
}

export { GraphDraftDetailCard, spokenReviewScript };
