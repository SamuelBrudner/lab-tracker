import * as React from "react";

import { formatDate } from "../../shared/formatters.js";
import { compactLabel } from "./capture-helpers.js";

// Presentational "Upload details" form: project + optional entity links for the
// capture. All selection state and setters are supplied by the controller.
function CaptureContextFields({
  canWrite,
  projects,
  selectedProjectId,
  onSelectedProjectChange,
  projectLocked = false,
  lockedCheckpointNoteId = "",
  activeQuestions,
  questionId,
  setQuestionId,
  sessions,
  sessionId,
  setSessionId,
  datasets,
  datasetId,
  setDatasetId,
  hint,
  setHint,
  analyses,
  analysisId,
  setAnalysisId,
  claims,
  claimId,
  setClaimId,
}) {
  return (
    <section className="capture-context-fields" aria-labelledby="capture-context-title">
      <div className="capture-section-head">
        <h3 id="capture-context-title">Upload details</h3>
      </div>
      <label>
        Project
        <select
          disabled={!canWrite || projectLocked}
          onChange={(event) => onSelectedProjectChange(event.target.value)}
          value={selectedProjectId}
        >
          <option value="">Choose project</option>
          {projects.map((project) => (
            <option key={project.project_id} value={project.project_id}>
              {project.name}
            </option>
          ))}
        </select>
      </label>
      {lockedCheckpointNoteId ? (
        <div className="item capture-locked-checkpoint" role="status">
          <strong>Tracking checkpoint attached</strong>
          <p className="subtle">
            This required note target is locked for your first forward capture. You can still add the ordinary context below.
          </p>
        </div>
      ) : null}
      <label>
        Active question (optional)
        <select
          disabled={!canWrite || !selectedProjectId}
          onChange={(event) => setQuestionId(event.target.value)}
          value={questionId}
        >
          <option value="">No question link</option>
          {activeQuestions.map((question) => (
            <option key={question.question_id} value={question.question_id}>
              {compactLabel(question.text)}
            </option>
          ))}
        </select>
      </label>
      <label>
        Session (optional)
        <select
          disabled={!canWrite || !selectedProjectId}
          onChange={(event) => setSessionId(event.target.value)}
          value={sessionId}
        >
          <option value="">No session link</option>
          {sessions.map((session) => (
            <option key={session.session_id} value={session.session_id}>
              {session.session_type} {formatDate(session.started_at)}
            </option>
          ))}
        </select>
      </label>
      <label>
        Dataset (optional)
        <select
          disabled={!canWrite || !selectedProjectId}
          onChange={(event) => setDatasetId(event.target.value)}
          value={datasetId}
        >
          <option value="">No dataset link</option>
          {datasets.map((dataset) => (
            <option key={dataset.dataset_id} value={dataset.dataset_id}>
              {dataset.commit_hash || dataset.dataset_id}
            </option>
          ))}
        </select>
      </label>
      <label>
        Short hint (optional)
        <textarea
          disabled={!canWrite}
          onChange={(event) => setHint(event.target.value)}
          placeholder="e.g. Rig 2, Fly 12, same gradient protocol as last week"
          value={hint}
        />
      </label>
      <details className="context-details">
        <summary>More context</summary>
        <label>
          Analysis (optional)
          <select
            disabled={!canWrite || !selectedProjectId}
            onChange={(event) => setAnalysisId(event.target.value)}
            value={analysisId}
          >
            <option value="">No analysis link</option>
            {analyses.map((analysis) => (
              <option key={analysis.analysis_id} value={analysis.analysis_id}>
                {compactLabel(analysis.method_hash || analysis.analysis_id)}
              </option>
            ))}
          </select>
        </label>
        <label>
          Claim (optional)
          <select
            disabled={!canWrite || !selectedProjectId}
            onChange={(event) => setClaimId(event.target.value)}
            value={claimId}
          >
            <option value="">No claim link</option>
            {claims.map((claim) => (
              <option key={claim.claim_id} value={claim.claim_id}>
                {compactLabel(claim.statement || claim.claim_id)}
              </option>
            ))}
          </select>
        </label>
      </details>
    </section>
  );
}

export { CaptureContextFields };
