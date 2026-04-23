import * as React from "react";

import { Dashboard } from "./features/dashboard-projects.jsx";
import { AnalysisPanel } from "./features/analysis/AnalysisPanel.jsx";
import { VisualizationDetailCard } from "./features/analysis/VisualizationDetailCard.jsx";
import { DatasetDetailCard, DatasetPanel } from "./features/datasets/index.js";
import { NoteDetailCard, NotePanel } from "./features/notes.jsx";
import { QuestionDetailCard } from "./features/questions/QuestionDetailCard.jsx";
import { QuestionPanel } from "./features/questions/QuestionPanel.jsx";
import { SessionDetailCard, SessionPanel } from "./features/sessions/index.js";
import { useAnalysisWorkflow } from "./hooks/useAnalysisWorkflow.js";
import { useAuthSession } from "./hooks/useAuthSession.js";
import { useDatasetWorkflow } from "./hooks/useDatasetWorkflow.js";
import { useProjectWorkspaceActions } from "./hooks/useProjectWorkspaceActions.js";
import { useProjectWorkspaceData } from "./hooks/useProjectWorkspaceData.js";
import { useProjectWorkspaceForms } from "./hooks/useProjectWorkspaceForms.js";
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
  const isHomeRoute = route.kind === "home";
  const [busy, setBusy] = React.useState(false);
  const [message, setMessage] = React.useState("");
  const [error, setError] = React.useState("");

  const setFlash = React.useCallback((nextMessage, nextError = "") => {
    setMessage(nextMessage);
    setError(nextError);
  }, []);

  const auth = useAuthSession({ replace, setBusy, setFlash });
  const workspaceData = useProjectWorkspaceData({
    loadProjectData: isHomeRoute,
    token: auth.token,
    setBusy,
    setFlash,
  });
  const workspaceForms = useProjectWorkspaceForms({
    questions: workspaceData.questions,
  });
  const workspaceActions = useProjectWorkspaceActions({
    token: auth.token,
    canWrite: auth.canWrite,
    selectedProjectId: workspaceData.selectedProjectId,
    refreshProjects: workspaceData.refreshProjects,
    refreshProjectData: workspaceData.refreshProjectData,
    setBusy,
    setFlash,
    setSelectedProjectId: workspaceData.setSelectedProjectId,
    setSessions: workspaceData.setSessions,
    projectName: workspaceForms.projectName,
    setProjectName: workspaceForms.setProjectName,
    projectDescription: workspaceForms.projectDescription,
    setProjectDescription: workspaceForms.setProjectDescription,
    questionText: workspaceForms.questionText,
    setQuestionText: workspaceForms.setQuestionText,
    questionType: workspaceForms.questionType,
    questionHypothesis: workspaceForms.questionHypothesis,
    setQuestionHypothesis: workspaceForms.setQuestionHypothesis,
    noteText: workspaceForms.noteText,
    setNoteText: workspaceForms.setNoteText,
    uploadFile: workspaceForms.uploadFile,
    setUploadFile: workspaceForms.setUploadFile,
    uploadTranscript: workspaceForms.uploadTranscript,
    setUploadTranscript: workspaceForms.setUploadTranscript,
    uploadTargetQuestionId: workspaceForms.uploadTargetQuestionId,
    setUploadTargetQuestionId: workspaceForms.setUploadTargetQuestionId,
    sessionType: workspaceForms.sessionType,
    sessionPrimaryQuestionId: workspaceForms.sessionPrimaryQuestionId,
  });
  const dataset = useDatasetWorkflow({
    token: auth.token,
    canWrite: auth.canWrite,
    selectedProjectId: workspaceData.selectedProjectId,
    questions: workspaceData.questions,
    refreshProjectData: workspaceData.refreshProjectData,
    setBusy,
    setFlash,
  });
  const analysis = useAnalysisWorkflow({
    enabled: isHomeRoute,
    token: auth.token,
    canWrite: auth.canWrite,
    selectedProjectId: workspaceData.selectedProjectId,
    setBusy,
    setFlash,
  });

  return (
    <div className="app-shell">
      <AppHeader user={auth.user} onLogout={auth.handleLogout} />

      <FlashMessages message={message} error={error} />

      {!auth.token ? (
        <section className="grid">
          <AuthForm
            authMode={auth.authMode}
            authUsername={auth.authUsername}
            authPassword={auth.authPassword}
            authBusy={auth.authBusy}
            onSubmit={auth.handleAuthSubmit}
            onUsernameChange={(event) => auth.setAuthUsername(event.target.value)}
            onPasswordChange={(event) => auth.setAuthPassword(event.target.value)}
            onToggleMode={() =>
              auth.setAuthMode((current) => (current === "login" ? "register" : "login"))
            }
          />
          <WorkflowCoverageCard />
        </section>
      ) : (
        <section className="grid">
          <Dashboard
            projects={workspaceData.projects}
            questionCount={workspaceData.questionCount}
            datasetCount={workspaceData.datasetCount}
            noteCount={workspaceData.noteCount}
            selectedProjectId={workspaceData.selectedProjectId}
            onSelectedProjectChange={(event) =>
              workspaceData.setSelectedProjectId(event.target.value)
            }
            canWrite={auth.canWrite}
            busy={busy}
            projectName={workspaceForms.projectName}
            projectDescription={workspaceForms.projectDescription}
            onProjectNameChange={(event) => workspaceForms.setProjectName(event.target.value)}
            onProjectDescriptionChange={(event) =>
              workspaceForms.setProjectDescription(event.target.value)
            }
            onCreateProject={workspaceActions.handleCreateProject}
          />

          {isHomeRoute ? (
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
              onCreateQuestion={workspaceActions.handleCreateQuestion}
              stagedQuestions={workspaceData.stagedQuestions}
              onActivateQuestion={workspaceActions.handleActivateQuestion}
            />
          ) : null}

          {isHomeRoute ? (
            <SessionPanel
              canWrite={auth.canWrite}
              busy={busy}
              projects={workspaceData.projects}
              selectedProjectId={workspaceData.selectedProjectId}
              onSelectedProjectChange={(event) =>
                workspaceData.setSelectedProjectId(event.target.value)
              }
              sessionType={workspaceForms.sessionType}
              onSessionTypeChange={(event) => workspaceForms.setSessionType(event.target.value)}
              sessionPrimaryQuestionId={workspaceForms.sessionPrimaryQuestionId}
              onSessionPrimaryQuestionIdChange={(event) =>
                workspaceForms.setSessionPrimaryQuestionId(event.target.value)
              }
              activeQuestions={workspaceData.activeQuestions}
              questions={workspaceData.questions}
              sessions={workspaceData.sessions}
              onCreateSession={workspaceActions.handleCreateSession}
              onCloseSession={workspaceActions.handleCloseSession}
              navigate={navigate}
            />
          ) : null}

          {route.kind === "question" ? (
            <QuestionDetailCard
              token={auth.token}
              questionId={route.questionId}
              projects={workspaceData.projects}
              navigate={navigate}
              onSetActiveProject={workspaceData.setSelectedProjectId}
            />
          ) : null}

          {route.kind === "note" ? (
            <NoteDetailCard
              token={auth.token}
              noteId={route.noteId}
              projects={workspaceData.projects}
              navigate={navigate}
              onSetActiveProject={workspaceData.setSelectedProjectId}
            />
          ) : null}

          {route.kind === "session" ? (
            <SessionDetailCard
              token={auth.token}
              sessionId={route.sessionId}
              projects={workspaceData.projects}
              navigate={navigate}
              onSetActiveProject={workspaceData.setSelectedProjectId}
              canWrite={auth.canWrite}
              onCloseSession={workspaceActions.handleCloseSession}
              onPromoteSession={workspaceActions.handlePromoteSession}
            />
          ) : null}

          {route.kind === "dataset" ? (
            <DatasetDetailCard
              token={auth.token}
              datasetId={route.datasetId}
              projects={workspaceData.projects}
              navigate={navigate}
              onSetActiveProject={workspaceData.setSelectedProjectId}
            />
          ) : null}

          {route.kind === "visualization" ? (
            <VisualizationDetailCard
              token={auth.token}
              vizId={route.vizId}
              navigate={navigate}
            />
          ) : null}

          {route.kind === "unknown" ? (
            <UnknownRouteCard pathname={route.pathname} navigate={navigate} />
          ) : null}

          {isHomeRoute ? (
            <NotePanel
              canWrite={auth.canWrite}
              busy={busy}
              selectedProjectId={workspaceData.selectedProjectId}
              noteText={workspaceForms.noteText}
              onNoteTextChange={(event) => workspaceForms.setNoteText(event.target.value)}
              onCreateTextNote={workspaceActions.handleCreateTextNote}
              onUploadNote={workspaceActions.handleUploadNote}
              onUploadFileChange={(event) =>
                workspaceForms.setUploadFile(event.target.files?.[0] || null)
              }
              uploadTargetQuestionId={workspaceForms.uploadTargetQuestionId}
              onUploadTargetQuestionIdChange={(event) =>
                workspaceForms.setUploadTargetQuestionId(event.target.value)
              }
              uploadTranscript={workspaceForms.uploadTranscript}
              onUploadTranscriptChange={(event) =>
                workspaceForms.setUploadTranscript(event.target.value)
              }
              activeQuestions={workspaceData.activeQuestions}
              notes={workspaceData.notes}
            />
          ) : null}

          {isHomeRoute ? (
            <DatasetPanel
              canWrite={auth.canWrite}
              busy={busy}
              selectedProjectId={workspaceData.selectedProjectId}
              datasetPrimaryQuestionId={dataset.datasetPrimaryQuestionId}
              onDatasetPrimaryQuestionIdChange={(event) =>
                dataset.setDatasetPrimaryQuestionId(event.target.value)
              }
              datasetSecondaryRaw={dataset.datasetSecondaryRaw}
              onDatasetSecondaryRawChange={(event) =>
                dataset.setDatasetSecondaryRaw(event.target.value)
              }
              onCreateDataset={dataset.handleCreateDataset}
              questions={workspaceData.questions}
              datasets={workspaceData.datasets}
              onCommitDataset={dataset.handleCommitDataset}
              datasetFilesById={dataset.datasetFilesById}
              onLoadDatasetFiles={dataset.loadDatasetFiles}
              onUploadDatasetFiles={dataset.handleUploadDatasetFiles}
              onDeleteDatasetFile={dataset.handleDeleteDatasetFile}
            />
          ) : null}

          {isHomeRoute ? (
            <AnalysisPanel
              canWrite={auth.canWrite}
              busy={busy}
              selectedProjectId={workspaceData.selectedProjectId}
              datasets={workspaceData.datasets}
              analyses={analysis.analyses}
              visualizations={analysis.visualizations}
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
              navigate={navigate}
            />
          ) : null}

          {isHomeRoute ? (
            <ProjectContextCard selectedProject={workspaceData.selectedProject} />
          ) : null}
        </section>
      )}

      {busy ? <p className="subtle">Syncing...</p> : null}
    </div>
  );
}

export { App };
