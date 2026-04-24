import * as React from "react";

import { Dashboard } from "../dashboard-projects.jsx";
import { AnalysisPanel } from "../analysis/AnalysisPanel.jsx";
import { DatasetPanel } from "../datasets/index.js";
import { NotePanel } from "../notes.jsx";
import { QuestionPanel } from "../questions/QuestionPanel.jsx";
import { SessionPanel } from "../sessions/index.js";
import { ProjectContextCard } from "../../shared/ui.jsx";

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
}) {
  return (
    <>
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
      />

      <QuestionPanel
        canWrite={auth.canWrite}
        busy={busy}
        selectedProjectId={workspaceData.selectedProjectId}
        questionText={workspaceForms.questionText}
        questionType={workspaceForms.questionType}
        questionHypothesis={workspaceForms.questionHypothesis}
        onQuestionTextChange={(event) => workspaceForms.setQuestionText(event.target.value)}
        onQuestionTypeChange={(event) => workspaceForms.setQuestionType(event.target.value)}
        onQuestionHypothesisChange={(event) =>
          workspaceForms.setQuestionHypothesis(event.target.value)
        }
        onCreateQuestion={questionActions.handleCreateQuestion}
        stagedQuestions={workspaceData.stagedQuestions}
        onActivateQuestion={questionActions.handleActivateQuestion}
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
        canWrite={auth.canWrite}
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
    </>
  );
}

export { WorkspaceHome };
