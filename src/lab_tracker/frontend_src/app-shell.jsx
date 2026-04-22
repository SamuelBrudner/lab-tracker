import * as React from "react";

import { Dashboard } from "./features/dashboard-projects.jsx";
import { AnalysisPanel, VisualizationDetailCard } from "./features/analysis-search.jsx";
import { DatasetDetailCard, DatasetPanel } from "./features/datasets.jsx";
import { NoteDetailCard, NotePanel } from "./features/notes.jsx";
import { QuestionDetailCard, QuestionPanel } from "./features/questions.jsx";
import { SessionDetailCard, SessionPanel } from "./features/sessions.jsx";
import { useAnalysisWorkflow } from "./hooks/useAnalysisWorkflow.js";
import { useAuthSession } from "./hooks/useAuthSession.js";
import { useDatasetWorkflow } from "./hooks/useDatasetWorkflow.js";
import { useProjectWorkspace } from "./hooks/useProjectWorkspace.js";
import {
  AppHeader,
  AuthForm,
  FlashMessages,
  ProjectContextCard,
  UnknownRouteCard,
  WorkflowCoverageCard,
} from "./shared/ui.jsx";
import { useAppRoute } from "./shared/routing.jsx";

function App() {
  const { navigate, replace, route } = useAppRoute();
  const [busy, setBusy] = React.useState(false);
  const [message, setMessage] = React.useState("");
  const [error, setError] = React.useState("");

  const setFlash = React.useCallback((nextMessage, nextError = "") => {
    setMessage(nextMessage);
    setError(nextError);
  }, []);

  const auth = useAuthSession({ replace, setBusy, setFlash });
  const workspace = useProjectWorkspace({
    token: auth.token,
    canWrite: auth.canWrite,
    setBusy,
    setFlash,
  });
  const dataset = useDatasetWorkflow({
    token: auth.token,
    canWrite: auth.canWrite,
    selectedProjectId: workspace.selectedProjectId,
    questions: workspace.questions,
    refreshProjectData: workspace.refreshProjectData,
    setBusy,
    setFlash,
  });
  const analysis = useAnalysisWorkflow({
    token: auth.token,
    canWrite: auth.canWrite,
    selectedProjectId: workspace.selectedProjectId,
    setBusy,
    setFlash,
  });
  const { refreshAnalysisData } = analysis;
  const { refreshProjectData, selectedProjectId } = workspace;

  const refreshActiveProject = React.useCallback(async () => {
    if (!auth.token || !selectedProjectId) {
      return { ok: true };
    }
    try {
      await Promise.all([
        refreshProjectData(selectedProjectId),
        refreshAnalysisData(selectedProjectId),
      ]);
      return { ok: true };
    } catch (err) {
      return {
        error: err.message || "Failed to refresh active project.",
        ok: false,
      };
    }
  }, [auth.token, refreshAnalysisData, refreshProjectData, selectedProjectId]);

  const state = {
    ...analysis,
    ...auth,
    ...dataset,
    ...workspace,
    busy,
    error,
    message,
    navigate,
    refreshActiveProject,
    route,
    setFlash,
  };

  return (
    <div className="app-shell">
      <AppHeader user={state.user} onLogout={state.handleLogout} />

      <FlashMessages message={state.message} error={state.error} />

      {!state.token ? (
        <section className="grid">
          <AuthForm
            authMode={state.authMode}
            authUsername={state.authUsername}
            authPassword={state.authPassword}
            authBusy={state.authBusy}
            onSubmit={state.handleAuthSubmit}
            onUsernameChange={(event) => state.setAuthUsername(event.target.value)}
            onPasswordChange={(event) => state.setAuthPassword(event.target.value)}
            onToggleMode={() =>
              state.setAuthMode((current) => (current === "login" ? "register" : "login"))
            }
          />
          <WorkflowCoverageCard />
        </section>
      ) : (
        <section className="grid">
          <Dashboard
            projects={state.projects}
            questions={state.questions}
            datasets={state.datasets}
            notes={state.notes}
            selectedProjectId={state.selectedProjectId}
            onSelectedProjectChange={(event) => state.setSelectedProjectId(event.target.value)}
            canWrite={state.canWrite}
            busy={state.busy}
            projectName={state.projectName}
            projectDescription={state.projectDescription}
            onProjectNameChange={(event) => state.setProjectName(event.target.value)}
            onProjectDescriptionChange={(event) => state.setProjectDescription(event.target.value)}
            onCreateProject={state.handleCreateProject}
          />

          {state.route.kind === "home" ? (
            <QuestionPanel
              canWrite={state.canWrite}
              busy={state.busy}
              selectedProjectId={state.selectedProjectId}
              questionText={state.questionText}
              questionType={state.questionType}
              questionHypothesis={state.questionHypothesis}
              onQuestionTextChange={(event) => state.setQuestionText(event.target.value)}
              onQuestionTypeChange={(event) => state.setQuestionType(event.target.value)}
              onQuestionHypothesisChange={(event) => state.setQuestionHypothesis(event.target.value)}
              onCreateQuestion={state.handleCreateQuestion}
              stagedQuestions={state.stagedQuestions}
              onActivateQuestion={state.handleActivateQuestion}
            />
          ) : null}

          {state.route.kind === "home" ? (
            <SessionPanel
              canWrite={state.canWrite}
              busy={state.busy}
              projects={state.projects}
              selectedProjectId={state.selectedProjectId}
              onSelectedProjectChange={(event) => state.setSelectedProjectId(event.target.value)}
              sessionType={state.sessionType}
              onSessionTypeChange={(event) => state.setSessionType(event.target.value)}
              sessionPrimaryQuestionId={state.sessionPrimaryQuestionId}
              onSessionPrimaryQuestionIdChange={(event) =>
                state.setSessionPrimaryQuestionId(event.target.value)
              }
              activeQuestions={state.activeQuestions}
              questions={state.questions}
              sessions={state.sessions}
              onCreateSession={state.handleCreateSession}
              onCloseSession={state.handleCloseSession}
              navigate={state.navigate}
            />
          ) : null}

          {state.route.kind === "question" ? (
            <QuestionDetailCard
              token={state.token}
              questionId={state.route.questionId}
              projects={state.projects}
              navigate={state.navigate}
              onSetActiveProject={state.setSelectedProjectId}
            />
          ) : null}

          {state.route.kind === "note" ? (
            <NoteDetailCard
              token={state.token}
              noteId={state.route.noteId}
              projects={state.projects}
              navigate={state.navigate}
              onSetActiveProject={state.setSelectedProjectId}
            />
          ) : null}

          {state.route.kind === "session" ? (
            <SessionDetailCard
              token={state.token}
              sessionId={state.route.sessionId}
              projects={state.projects}
              questions={state.questions}
              navigate={state.navigate}
              onSetActiveProject={state.setSelectedProjectId}
              canWrite={state.canWrite}
              onCloseSession={state.handleCloseSession}
              onPromoteSession={state.handlePromoteSession}
            />
          ) : null}

          {state.route.kind === "dataset" ? (
            <DatasetDetailCard
              token={state.token}
              datasetId={state.route.datasetId}
              projects={state.projects}
              navigate={state.navigate}
              onSetActiveProject={state.setSelectedProjectId}
            />
          ) : null}

          {state.route.kind === "visualization" ? (
            <VisualizationDetailCard
              token={state.token}
              vizId={state.route.vizId}
              navigate={state.navigate}
            />
          ) : null}

          {state.route.kind === "unknown" ? (
            <UnknownRouteCard pathname={state.route.pathname} navigate={state.navigate} />
          ) : null}

          {state.route.kind === "home" ? (
            <NotePanel
              canWrite={state.canWrite}
              busy={state.busy}
              selectedProjectId={state.selectedProjectId}
              noteText={state.noteText}
              onNoteTextChange={(event) => state.setNoteText(event.target.value)}
              onCreateTextNote={state.handleCreateTextNote}
              onUploadNote={state.handleUploadNote}
              onUploadFileChange={(event) => state.setUploadFile(event.target.files?.[0] || null)}
              uploadTargetQuestionId={state.uploadTargetQuestionId}
              onUploadTargetQuestionIdChange={(event) =>
                state.setUploadTargetQuestionId(event.target.value)
              }
              uploadTranscript={state.uploadTranscript}
              onUploadTranscriptChange={(event) => state.setUploadTranscript(event.target.value)}
              activeQuestions={state.activeQuestions}
              notes={state.notes}
            />
          ) : null}

          {state.route.kind === "home" ? (
            <DatasetPanel
              canWrite={state.canWrite}
              busy={state.busy}
              selectedProjectId={state.selectedProjectId}
              datasetPrimaryQuestionId={state.datasetPrimaryQuestionId}
              onDatasetPrimaryQuestionIdChange={(event) =>
                state.setDatasetPrimaryQuestionId(event.target.value)
              }
              datasetSecondaryRaw={state.datasetSecondaryRaw}
              onDatasetSecondaryRawChange={(event) => state.setDatasetSecondaryRaw(event.target.value)}
              onCreateDataset={state.handleCreateDataset}
              questions={state.questions}
              datasets={state.datasets}
              onCommitDataset={state.handleCommitDataset}
              datasetFilesById={state.datasetFilesById}
              onLoadDatasetFiles={state.loadDatasetFiles}
              onUploadDatasetFiles={state.handleUploadDatasetFiles}
              onDeleteDatasetFile={state.handleDeleteDatasetFile}
            />
          ) : null}

          {state.route.kind === "home" ? (
            <AnalysisPanel
              canWrite={state.canWrite}
              busy={state.busy}
              selectedProjectId={state.selectedProjectId}
              datasets={state.datasets}
              analyses={state.analyses}
              visualizations={state.visualizations}
              analysisDatasetIds={state.analysisDatasetIds}
              analysisCodeVersion={state.analysisCodeVersion}
              analysisMethodHash={state.analysisMethodHash}
              analysisEnvironmentHash={state.analysisEnvironmentHash}
              onAnalysisDatasetIdsChange={(event) => {
                const selected = Array.from(event.target.selectedOptions || []).map(
                  (option) => option.value
                );
                state.setAnalysisDatasetIds(selected);
              }}
              onAnalysisCodeVersionChange={(event) =>
                state.setAnalysisCodeVersion(event.target.value)
              }
              onAnalysisMethodHashChange={(event) =>
                state.setAnalysisMethodHash(event.target.value)
              }
              onAnalysisEnvironmentHashChange={(event) =>
                state.setAnalysisEnvironmentHash(event.target.value)
              }
              onCreateAnalysis={state.handleCreateAnalysis}
              onCommitAnalysis={state.handleCommitAnalysis}
              onArchiveAnalysis={state.handleArchiveAnalysis}
              navigate={state.navigate}
            />
          ) : null}

          {state.route.kind === "home" ? (
            <ProjectContextCard selectedProject={state.selectedProject} />
          ) : null}
        </section>
      )}

      {state.busy ? <p className="subtle">Syncing...</p> : null}
    </div>
  );
}

export { App };
