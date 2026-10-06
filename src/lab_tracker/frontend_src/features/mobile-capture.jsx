import * as React from "react";

import { useMobileCapture } from "../hooks/useMobileCapture.js";
import { sessionLabel } from "./bench-capture/bench-helpers.js";
import { PhotoImportPanel } from "./bench-capture/PhotoImportPanel.jsx";
import { SessionDebrief } from "./bench-capture/SessionDebrief.jsx";
import { TrustedShareBanner } from "./bench-capture/TrustedShareBanner.jsx";
import { CaptureComposer } from "./mobile-capture/CaptureComposer.jsx";
import { CaptureContextFields } from "./mobile-capture/CaptureContextFields.jsx";
import { MobileInstallPrompt } from "./mobile-capture/MobileInstallPrompt.jsx";
import { PendingReviewList } from "./mobile-capture/PendingReviewList.jsx";
import { SharedInboxReview } from "./mobile-capture/SharedInboxReview.jsx";
import { readCaptureLaunchContext } from "./mobile-capture/capture-helpers.js";

function MobileCaptureCard({
  token,
  ownerId = "",
  authEnabled = true,
  canWrite,
  projects,
  selectedProjectId,
  onSelectedProjectChange,
  questions,
  datasets,
  sessions,
  navigate,
  setBusy,
  setFlash,
  refreshProjectCounts,
  refreshRecentNotes,
}) {
  const captureSearch = window.location.search;
  const launchContext = React.useMemo(
    () => readCaptureLaunchContext(captureSearch),
    [captureSearch]
  );
  const capture = useMobileCapture({
    token,
    ownerId,
    authEnabled,
    canWrite,
    selectedProjectId,
    questions,
    sessions,
    navigate,
    setBusy,
    setFlash,
    refreshProjectCounts,
    refreshRecentNotes,
    lockedCheckpointNoteId: launchContext.checkpointNoteId,
    launchSessionId: launchContext.sessionId,
    launchCaptureChannel: launchContext.captureChannel,
    returnPath: launchContext.returnPath,
  });
  const [debriefOpen, setDebriefOpen] = React.useState(false);
  const selectedSession =
    sessions.find((session) => session.session_id === capture.sessionId) || null;

  return (
    <article className="card span-12 capture-card">
      <MobileInstallPrompt />

      <TrustedShareBanner
        trust={capture.shareTrust}
        checkedAt={capture.shareTrustCheckedAt}
        onStop={capture.stopShareTrust}
      />

      <SharedInboxReview
        shares={capture.incomingShares}
        projects={projects}
        selectedProjectId={selectedProjectId}
        canWrite={canWrite}
        busy={capture.sharesBusy}
        onImport={capture.importIncomingShares}
        onDiscard={capture.discardIncomingShares}
        trustSessionLabel={selectedSession && !capture.shareTrust ? sessionLabel(selectedSession) : ""}
        onTrust={capture.trustShares}
      />

      {capture.clip ? (
        <p className="subtle capture-clip-notice" role="status">
          Prefilled from {capture.clip.title || capture.clip.url || "the page you were on"}.
          Nothing is saved until you press send.
        </p>
      ) : null}

      <div className="capture-layout">
        <div className="stack">
          <form className="form capture-form" onSubmit={(event) => event.preventDefault()}>
            <CaptureComposer
              canWrite={canWrite}
              navigate={navigate}
              returnPath={launchContext.returnPath}
              attachmentMenuOpen={capture.attachmentMenuOpen}
              setAttachmentMenuOpen={capture.setAttachmentMenuOpen}
              photoFile={capture.photoFile}
              audioFile={capture.audioFile}
              captureMode={capture.captureMode}
              composerTextValue={capture.composerTextValue()}
              onComposerTextChange={capture.handleComposerTextChange}
              onPhotoFileChange={capture.handlePhotoFileChange}
              onAudioFileChange={capture.handleAudioFileChange}
              onClearPhotoFile={capture.clearPhotoFile}
              onClearAudioFile={capture.clearAudioFile}
              onStartTextCapture={capture.startTextCapture}
              onStartBundleCapture={capture.startBundleCapture}
              readyToCapture={capture.readyToCapture()}
              uploading={capture.uploading}
              needsVoice={capture.needsVoice()}
              voiceNoteType={capture.voiceNoteType}
              setVoiceNoteType={capture.setVoiceNoteType}
              onUploadCapture={capture.uploadCapture}
              draftSavedAt={capture.composerDraftSavedAt}
              onRestoreDraft={capture.restoreComposerText}
              onDiscardDraft={capture.discardComposerDraft}
              recordingSupported={capture.recordingSupported}
              isRecording={capture.isRecording}
              onToggleRecording={capture.toggleRecording}
            />
  
            <CaptureContextFields
              canWrite={canWrite}
              projects={projects}
              selectedProjectId={selectedProjectId}
              onSelectedProjectChange={onSelectedProjectChange}
              projectLocked={Boolean(launchContext.checkpointNoteId)}
              lockedCheckpointNoteId={launchContext.checkpointNoteId}
              activeQuestions={capture.activeQuestions}
              questionId={capture.questionId}
              setQuestionId={capture.setQuestionId}
              sessions={sessions}
              sessionId={capture.sessionId}
              setSessionId={capture.setSessionId}
              contextCarriedOver={capture.contextCarriedOver}
              datasets={datasets}
              datasetId={capture.datasetId}
              setDatasetId={capture.setDatasetId}
              hint={capture.hint}
              setHint={capture.setHint}
              analyses={capture.analyses}
              analysisId={capture.analysisId}
              setAnalysisId={capture.setAnalysisId}
              claims={capture.claims}
              claimId={capture.claimId}
              setClaimId={capture.setClaimId}
            />
          </form>
  
          {selectedSession && canWrite ? (
            <section className="stack capture-session-tools" aria-label="Session tools">
              <PhotoImportPanel
                token={token}
                ownerId={ownerId}
                projectId={selectedProjectId}
                session={selectedSession}
                canWrite={canWrite}
              />
              {debriefOpen ? (
                <SessionDebrief
                  token={token}
                  ownerId={ownerId}
                  projectId={selectedProjectId}
                  session={selectedSession}
                  canWrite={canWrite}
                  onDone={() => setDebriefOpen(false)}
                />
              ) : (
                <div className="inline">
                  <button
                    type="button"
                    className="btn-secondary"
                    onClick={() => setDebriefOpen(true)}
                  >
                    Debrief
                  </button>
                </div>
              )}
            </section>
          ) : null}
        </div>

        <PendingReviewList
          pendingError={capture.pendingError}
          pendingDrafts={capture.pendingDrafts}
          pendingNotes={capture.pendingNotes}
          pendingActionById={capture.pendingActionById}
          pendingActionErrors={capture.pendingActionErrors}
          canWrite={canWrite}
          navigate={navigate}
          onTranscribe={capture.transcribePendingNote}
        />
      </div>
    </article>
  );
}

export { MobileCaptureCard };
