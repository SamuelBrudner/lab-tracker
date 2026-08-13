import * as React from "react";

import { useMobileCapture } from "../hooks/useMobileCapture.js";
import { CaptureComposer } from "./mobile-capture/CaptureComposer.jsx";
import { CaptureContextFields } from "./mobile-capture/CaptureContextFields.jsx";
import { MobileInstallPrompt } from "./mobile-capture/MobileInstallPrompt.jsx";
import { PendingReviewList } from "./mobile-capture/PendingReviewList.jsx";
import { readCaptureLaunchContext } from "./mobile-capture/capture-helpers.js";

function MobileCaptureCard({
  token,
  ownerId = "",
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
    canWrite,
    selectedProjectId,
    questions,
    navigate,
    setBusy,
    setFlash,
    refreshProjectCounts,
    refreshRecentNotes,
    lockedCheckpointNoteId: launchContext.checkpointNoteId,
    returnPath: launchContext.returnPath,
  });

  return (
    <article className="card span-12 capture-card">
      <MobileInstallPrompt />

      <div className="capture-layout">
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
            needsVoice={capture.needsVoice()}
            voiceNoteType={capture.voiceNoteType}
            setVoiceNoteType={capture.setVoiceNoteType}
            onUploadCapture={capture.uploadCapture}
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
