import * as React from "react";

import { Dashboard } from "../dashboard-projects.jsx";
import { AnalysisPanel } from "../analysis/AnalysisPanel.jsx";
import { DatasetPanel } from "../datasets/index.js";
import { NotePanel } from "../notes.jsx";
import { PortfolioHome } from "../portfolio-home.jsx";
import { QuestionPanel } from "../questions/QuestionPanel.jsx";
import { SessionPanel } from "../sessions/index.js";
import { ProjectContextCard, RequestEditAccess } from "../../shared/ui.jsx";

function WorkspaceHome({
  auth,
  busy,
  navigate,
  workspaceData,
  workspaceForms,
  projectActions,
  questionActions,
  noteActions,
  noteData,
  sessionActions,
  sessionData,
  dataset,
  analysis,
  projectMembers = [],
  projectAccess = {},
}) {
  const canContribute = Boolean(projectAccess.canContribute);
  const hasProjects = workspaceData.projects.length > 0;
  const showPortfolioHome = workspaceData.projects.length > 1;
  return (
    <>
      <PortfolioHome
        authEnabled={auth.authEnabled}
        enabled={showPortfolioHome}
        token={auth.token}
        onOpenProject={workspaceData.setSelectedProjectId}
      />
      {!hasProjects ? (
        <article className="card span-12 first-run-card">
          <div>
            <h2>Welcome to Lab Tracker</h2>
            <p className="subtle">Create your first project to start capturing lab context.</p>
          </div>
          <button
            type="button"
            className="btn-primary"
            disabled={!auth.canWrite}
            onClick={() => document.querySelector('[name="new-project-name"]')?.focus()}
          >
            Create first project
          </button>
        </article>
      ) : null}

      <Dashboard
        projects={workspaceData.projects}
        questionCount={workspaceData.questionCount}
        datasetCount={workspaceData.datasetCount}
        noteCount={workspaceData.noteCount}
        selectedProjectId={workspaceData.selectedProjectId}
        onSelectedProjectChange={(event) => workspaceData.setSelectedProjectId(event.target.value)}
        canWrite={auth.canWrite}
        busy={busy}
        projectName={workspaceForms.projectName}
        projectDescription={workspaceForms.projectDescription}
        onProjectNameChange={(event) => workspaceForms.setProjectName(event.target.value)}
        onProjectDescriptionChange={(event) =>
          workspaceForms.setProjectDescription(event.target.value)
        }
        onCreateProject={projectActions.handleCreateProject}
        onOpenGraph={() => navigate("/app/graph")}
        onOpenBatches={() => navigate("/app/batches")}
        onOpenReviewQueue={() => navigate("/app/review")}
        projectMembers={projectMembers}
        canManageProjectMembers={projectAccess.canManageMembers}
        memberUsername={projectAccess.memberUsername || ""}
        memberRole={projectAccess.memberRole || "contributor"}
        onMemberUsernameChange={projectAccess.onMemberUsernameChange}
        onMemberRoleChange={projectAccess.onMemberRoleChange}
        onAddProjectMember={projectAccess.onAddMember}
        onUpdateProjectMember={projectAccess.onUpdateMember}
        onRemoveProjectMember={projectAccess.onRemoveMember}
        requestAccessNode={
          <RequestEditAccess selectedProject={workspaceData.selectedProject} />
        }
      />

      <QuestionPanel
        canWrite={auth.canWrite}
        busy={busy}
        selectedProjectId={workspaceData.selectedProjectId}
        questionText={workspaceForms.questionText}
        questionType={workspaceForms.questionType}
        questionHypothesis={workspaceForms.questionHypothesis}
        questionParentIds={workspaceForms.questionParentIds}
        onQuestionTextChange={(event) => workspaceForms.setQuestionText(event.target.value)}
        onQuestionTypeChange={(event) => workspaceForms.setQuestionType(event.target.value)}
        onQuestionHypothesisChange={(event) =>
          workspaceForms.setQuestionHypothesis(event.target.value)
        }
        onQuestionParentIdsChange={(event) => {
          const selected = Array.from(event.target.selectedOptions || []).map(
            (option) => option.value
          );
          workspaceForms.setQuestionParentIds(selected);
        }}
        onCreateQuestion={questionActions.handleCreateQuestion}
        questions={workspaceData.questions}
        stagedQuestions={workspaceData.stagedQuestions}
        onActivateQuestion={questionActions.handleActivateQuestion}
        navigate={navigate}
      />

      <SessionPanel
        canWrite={auth.canWrite}
        busy={busy}
        error={sessionData.error}
        loading={sessionData.loading}
        projects={workspaceData.projects}
        selectedProjectId={workspaceData.selectedProjectId}
        onSelectedProjectChange={(event) => workspaceData.setSelectedProjectId(event.target.value)}
        sessionType={workspaceForms.sessionType}
        onSessionTypeChange={(event) => workspaceForms.setSessionType(event.target.value)}
        sessionPrimaryQuestionId={workspaceForms.sessionPrimaryQuestionId}
        onSessionPrimaryQuestionIdChange={(event) =>
          workspaceForms.setSessionPrimaryQuestionId(event.target.value)
        }
        activeQuestions={workspaceData.activeQuestions}
        questions={workspaceData.questions}
        sessions={sessionData.sessions}
        onCreateSession={sessionActions.handleCreateSession}
        onCloseSession={sessionActions.handleCloseSession}
        navigate={navigate}
      />

      <NotePanel
        canWrite={canContribute}
        busy={busy}
        error={noteData.error}
        loading={noteData.loading}
        selectedProjectId={workspaceData.selectedProjectId}
        noteText={workspaceForms.noteText}
        onNoteTextChange={(event) => workspaceForms.setNoteText(event.target.value)}
        onCreateTextNote={noteActions.handleCreateTextNote}
        onUploadNote={noteActions.handleUploadNote}
        onUploadFileChange={(event) => workspaceForms.setUploadFile(event.target.files?.[0] || null)}
        uploadTargetQuestionId={workspaceForms.uploadTargetQuestionId}
        onUploadTargetQuestionIdChange={(event) =>
          workspaceForms.setUploadTargetQuestionId(event.target.value)
        }
        uploadTranscript={workspaceForms.uploadTranscript}
        onUploadTranscriptChange={(event) =>
          workspaceForms.setUploadTranscript(event.target.value)
        }
        activeQuestions={workspaceData.activeQuestions}
        notes={noteData.notes}
        navigate={navigate}
      />

      <DatasetPanel
        canWrite={auth.canWrite}
        busy={busy}
        selectedProjectId={workspaceData.selectedProjectId}
        datasetPrimaryQuestionId={dataset.datasetPrimaryQuestionId}
        onDatasetPrimaryQuestionIdChange={(event) =>
          dataset.setDatasetPrimaryQuestionId(event.target.value)
        }
        datasetSecondaryRaw={dataset.datasetSecondaryRaw}
        onDatasetSecondaryRawChange={(event) => dataset.setDatasetSecondaryRaw(event.target.value)}
        onCreateDataset={dataset.handleCreateDataset}
        questions={workspaceData.activeQuestions}
        datasets={workspaceData.stagedDatasets}
        onCommitDataset={dataset.handleCommitDataset}
        datasetFilesById={dataset.datasetFilesById}
        onLoadDatasetFiles={dataset.loadDatasetFiles}
        onUploadDatasetFiles={dataset.handleUploadDatasetFiles}
        onDeleteDatasetFile={dataset.handleDeleteDatasetFile}
      />

      <AnalysisPanel
        canWrite={auth.canWrite}
        busy={busy}
        error={analysis.analysisError}
        loading={analysis.analysisLoading}
        selectedProjectId={workspaceData.selectedProjectId}
        datasets={workspaceData.datasets}
        stagedAnalyses={analysis.stagedAnalyses}
        recentCommittedAnalyses={analysis.recentCommittedAnalyses}
        visualizationStates={analysis.visualizationStates}
        analysisDatasetIds={analysis.analysisDatasetIds}
        analysisCodeVersion={analysis.analysisCodeVersion}
        analysisMethodHash={analysis.analysisMethodHash}
        analysisEnvironmentHash={analysis.analysisEnvironmentHash}
        onAnalysisDatasetIdsChange={(event) => {
          const selected = Array.from(event.target.selectedOptions || []).map(
            (option) => option.value
          );
          analysis.setAnalysisDatasetIds(selected);
        }}
        onAnalysisCodeVersionChange={(event) =>
          analysis.setAnalysisCodeVersion(event.target.value)
        }
        onAnalysisMethodHashChange={(event) =>
          analysis.setAnalysisMethodHash(event.target.value)
        }
        onAnalysisEnvironmentHashChange={(event) =>
          analysis.setAnalysisEnvironmentHash(event.target.value)
        }
        onCreateAnalysis={analysis.handleCreateAnalysis}
        onCommitAnalysis={analysis.handleCommitAnalysis}
        onArchiveAnalysis={analysis.handleArchiveAnalysis}
        onLoadVisualizations={analysis.loadVisualizations}
        navigate={navigate}
      />

      <ProjectContextCard selectedProject={workspaceData.selectedProject} />

      <article className="card span-12">
        <h2>Terms</h2>
        <div className="term-grid">
          <p>
            <strong>Project</strong>
            <span>One durable research effort or lab question space.</span>
          </p>
          <p>
            <strong>Question</strong>
            <span>A staged or active scientific question that records why data was collected.</span>
          </p>
          <p>
            <strong>Dataset</strong>
            <span>A committed data record with provenance and source files.</span>
          </p>
          <p>
            <strong>Claim</strong>
            <span>An interpretation linked back to datasets or analyses.</span>
          </p>
          <p>
            <strong>Graph draft</strong>
            <span>A proposed change that a human reviews before it is committed.</span>
          </p>
        </div>
      </article>
    </>
  );
}

export { WorkspaceHome };
