import * as React from "react";

import { useGraphDraftWorkflow } from "../hooks/useGraphDraftWorkflow.js";
import { useReviewDictation } from "../hooks/useReviewDictation.js";
import { useSourceArtifactPreviews } from "../hooks/useSourceArtifactPreviews.js";
import { AudioReviewConsole } from "./graph-drafts/AudioReviewConsole.jsx";
import { NarrativeReview } from "./graph-drafts/NarrativeReview.jsx";
import { OperationRow } from "./graph-drafts/OperationRow.jsx";
import { ProvenanceDetails } from "./graph-drafts/ProvenanceDetails.jsx";
import { SourceArtifactEvidence } from "./graph-drafts/SourceArtifactEvidence.jsx";
import { spokenReviewScript } from "./graph-drafts/format.js";
import { buildSourceArtifactReview } from "./graph-drafts/source-artifacts.js";

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
    commitMessage,
    setCommitMessage,
    reviewNote,
    setReviewNote,
    pendingCommands,
    acceptedCount,
    undoableOperationIds,
    canEditDraft,
    canSubmitDraft,
    canReviewDraft,
    canCommitDraft,
  } = workflow;
  const sourceReview = React.useMemo(
    () => buildSourceArtifactReview(changeSet),
    [changeSet]
  );
  const sourcePreviews = useSourceArtifactPreviews(sourceReview.artifactsToLoad, token);
  const reviewAttachmentEvidence = changeSet?.context_packet?.review_attachment_evidence;
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
            canEditDraft={canEditDraft}
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
              {(changeSet.operations || []).map((operation) => {
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
              <strong>
                {acceptedCount} of {(changeSet.operations || []).length} kept
              </strong>
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
            </div>

            {canEditDraft && undoableOperationIds.length > 0 ? (
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
                Commit message
                <input
                  value={commitMessage}
                  onChange={(event) => setCommitMessage(event.target.value)}
                  disabled={!canCommitDraft}
                />
              </label>
              <button
                className="btn-primary"
                disabled={!canCommitDraft || acceptedCount === 0 || Boolean(pendingCommands.commit)}
              >
                Commit accepted changes
              </button>
            </form>
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
