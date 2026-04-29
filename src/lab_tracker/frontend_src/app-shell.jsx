import * as React from "react";

import { Dashboard } from "./features/dashboard-projects.jsx";
import { GraphDraftDetailCard } from "./features/graph-drafts.jsx";
import { VisualizationDetailCard } from "./features/analysis/VisualizationDetailCard.jsx";
import { DatasetDetailCard } from "./features/datasets/index.js";
import { NoteDetailCard } from "./features/notes.jsx";
import { QuestionDetailCard } from "./features/questions/QuestionDetailCard.jsx";
import { SessionDetailCard } from "./features/sessions/index.js";
import { WorkspaceHome } from "./features/workspace/WorkspaceHome.jsx";
import { useAnalysisWorkflow } from "./hooks/useAnalysisWorkflow.js";
import { useAuthSession } from "./hooks/useAuthSession.js";
import { useDatasetWorkflow } from "./hooks/useDatasetWorkflow.js";
import { useNoteActions } from "./hooks/useNoteActions.js";
import { useProjectActions } from "./hooks/useProjectActions.js";
import { useProjectNoteData } from "./hooks/useProjectNoteData.js";
import { useProjectSessionData } from "./hooks/useProjectSessionData.js";
import { useProjectWorkspaceData } from "./hooks/useProjectWorkspaceData.js";
import { useProjectWorkspaceForms } from "./hooks/useProjectWorkspaceForms.js";
import { useQuestionActions } from "./hooks/useQuestionActions.js";
import { useSessionActions } from "./hooks/useSessionActions.js";
import {
  AppHeader,
  AuthForm,
  FlashMessages,
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
  const noteData = useProjectNoteData({
    enabled: isHomeRoute,
    selectedProjectId: workspaceData.selectedProjectId,
    setFlash,
    token: auth.token,
  });
  const sessionData = useProjectSessionData({
    enabled: isHomeRoute,
    selectedProjectId: workspaceData.selectedProjectId,
    setFlash,
    token: auth.token,
  });
  const workspaceForms = useProjectWorkspaceForms({
    questions: workspaceData.questions,
  });
  const projectActions = useProjectActions({
    token: auth.token,
    canWrite: auth.canWrite,
    refreshProjects: workspaceData.refreshProjects,
    setBusy,
    setFlash,
    setSelectedProjectId: workspaceData.setSelectedProjectId,
    projectName: workspaceForms.projectName,
    setProjectName: workspaceForms.setProjectName,
    projectDescription: workspaceForms.projectDescription,
    setProjectDescription: workspaceForms.setProjectDescription,
  });
  const questionActions = useQuestionActions({
    token: auth.token,
    canWrite: auth.canWrite,
    selectedProjectId: workspaceData.selectedProjectId,
    refreshProjectData: workspaceData.refreshProjectData,
    setBusy,
    setFlash,
    questionText: workspaceForms.questionText,
    setQuestionText: workspaceForms.setQuestionText,
    questionType: workspaceForms.questionType,
    questionHypothesis: workspaceForms.questionHypothesis,
    setQuestionHypothesis: workspaceForms.setQuestionHypothesis,
  });
  const noteActions = useNoteActions({
    token: auth.token,
    canWrite: auth.canWrite,
    selectedProjectId: workspaceData.selectedProjectId,
    refreshProjectCounts: workspaceData.refreshProjectCounts,
    refreshRecentNotes: noteData.refreshRecentNotes,
    setBusy,
    setFlash,
    noteText: workspaceForms.noteText,
    setNoteText: workspaceForms.setNoteText,
    uploadFile: workspaceForms.uploadFile,
    setUploadFile: workspaceForms.setUploadFile,
    uploadTranscript: workspaceForms.uploadTranscript,
    setUploadTranscript: workspaceForms.setUploadTranscript,
    uploadTargetQuestionId: workspaceForms.uploadTargetQuestionId,
    setUploadTargetQuestionId: workspaceForms.setUploadTargetQuestionId,
  });
  const sessionActions = useSessionActions({
    token: auth.token,
    canWrite: auth.canWrite,
    selectedProjectId: workspaceData.selectedProjectId,
    refreshActiveSessions: sessionData.refreshActiveSessions,
    setBusy,
    setFlash,
    setSessions: sessionData.setSessions,
    sessionType: workspaceForms.sessionType,
    sessionPrimaryQuestionId: workspaceForms.sessionPrimaryQuestionId,
  });
  const dataset = useDatasetWorkflow({
    token: auth.token,
    canWrite: auth.canWrite,
    selectedProjectId: workspaceData.selectedProjectId,
    questions: workspaceData.questions,
    datasets: workspaceData.stagedDatasets,
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
  const dashboardProps = {
    projects: workspaceData.projects,
    questionCount: workspaceData.questionCount,
    datasetCount: workspaceData.datasetCount,
    noteCount: workspaceData.noteCount,
    selectedProjectId: workspaceData.selectedProjectId,
    onSelectedProjectChange: (event) => workspaceData.setSelectedProjectId(event.target.value),
    canWrite: auth.canWrite,
    busy,
    projectName: workspaceForms.projectName,
    projectDescription: workspaceForms.projectDescription,
    onProjectNameChange: (event) => workspaceForms.setProjectName(event.target.value),
    onProjectDescriptionChange: (event) =>
      workspaceForms.setProjectDescription(event.target.value),
    onCreateProject: projectActions.handleCreateProject,
  };

  return (
    <div className="app-shell">
      <AppHeader
        authEnabled={auth.authEnabled}
        user={auth.user}
        onLogout={auth.handleLogout}
      />

      <FlashMessages message={message} error={error} />

      {!auth.authChecked ? (
        <section className="grid">
          <WorkflowCoverageCard />
        </section>
      ) : auth.authEnabled && !auth.token ? (
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
          {isHomeRoute ? (
            <WorkspaceHome
              auth={auth}
              busy={busy}
              navigate={navigate}
              workspaceData={workspaceData}
              workspaceForms={workspaceForms}
              projectActions={projectActions}
              questionActions={questionActions}
              noteActions={noteActions}
              noteData={noteData}
              sessionActions={sessionActions}
              sessionData={sessionData}
              dataset={dataset}
              analysis={analysis}
            />
          ) : (
            <Dashboard {...dashboardProps} />
          )}

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
              canWrite={auth.canWrite}
              setBusy={setBusy}
              setFlash={setFlash}
            />
          ) : null}

          {route.kind === "graph-draft" ? (
            <GraphDraftDetailCard
              token={auth.token}
              changeSetId={route.changeSetId}
              navigate={navigate}
              canWrite={auth.canWrite}
              setBusy={setBusy}
              setFlash={setFlash}
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
              onCloseSession={sessionActions.handleCloseSession}
              onPromoteSession={sessionActions.handlePromoteSession}
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
        </section>
      )}

      {busy ? <p className="subtle">Syncing...</p> : null}
    </div>
  );
}

export { App };
