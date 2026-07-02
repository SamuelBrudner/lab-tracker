import * as React from "react";

import { Dashboard } from "./features/dashboard-projects.jsx";
import { BatchReviewPage, PendingBatchBanner } from "./features/batches.jsx";
import { DevicesPage } from "./features/devices.jsx";
import { EnrollPage } from "./features/enroll.jsx";
import { GraphDraftDetailCard } from "./features/graph-drafts.jsx";
import { GoalDetailCard } from "./features/goals/GoalDetailCard.jsx";
import { ProjectGraphExplorer } from "./features/project-graph.jsx";
import { VisualizationDetailCard } from "./features/analysis/VisualizationDetailCard.jsx";
import { DatasetDetailCard } from "./features/datasets/index.js";
import { MobileCaptureCard } from "./features/mobile-capture.jsx";
import { NoteDetailCard } from "./features/notes.jsx";
import { QuestionDetailCard } from "./features/questions/QuestionDetailCard.jsx";
import { SessionDetailCard } from "./features/sessions/index.js";
import { UsersPage } from "./features/users.jsx";
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
import { droppedUploadsMessage, installOfflineRetry } from "./shared/register-sw.js";
import { PendingUploadsBadge } from "./shared/upload-status.jsx";
import { apiListRequest, apiRequest, buildApiPath } from "./shared/api.js";

function App() {
  const { navigate, replace, route } = useAppRoute();
  const isHomeRoute = route.kind === "home";
  const needsProjectData = isHomeRoute || route.kind === "capture" || route.kind === "batches";
  const [busy, setBusy] = React.useState(false);
  const [message, setMessage] = React.useState("");
  const [error, setError] = React.useState("");

  const setFlash = React.useCallback((nextMessage, nextError = "") => {
    setMessage(nextMessage);
    setError(nextError);
  }, []);

  const auth = useAuthSession({ replace, setBusy, setFlash });
  const apiEnabled = auth.authChecked && (!auth.authEnabled || Boolean(auth.token));
  React.useEffect(() => {
    if (!auth.authChecked) {
      return undefined;
    }
    return installOfflineRetry({
      getToken: () => auth.token,
      onDropped: (dropped) => {
        setFlash("", droppedUploadsMessage(dropped));
      },
    });
  }, [auth.authChecked, auth.token, setFlash]);
  const [projectMembers, setProjectMembers] = React.useState([]);
  const [memberUsername, setMemberUsername] = React.useState("");
  const [memberRole, setMemberRole] = React.useState("contributor");
  const workspaceData = useProjectWorkspaceData({
    enabled: apiEnabled,
    loadProjectData: needsProjectData,
    token: auth.token,
    setBusy,
    setFlash,
  });
  const noteData = useProjectNoteData({
    enabled: isHomeRoute && apiEnabled,
    selectedProjectId: workspaceData.selectedProjectId,
    setFlash,
    token: auth.token,
  });
  const sessionData = useProjectSessionData({
    enabled: needsProjectData && apiEnabled,
    selectedProjectId: workspaceData.selectedProjectId,
    setFlash,
    token: auth.token,
  });
  const workspaceForms = useProjectWorkspaceForms({
    questions: workspaceData.questions,
  });
  const refreshProjectMembers = React.useCallback(async () => {
    if (!apiEnabled || !workspaceData.selectedProjectId) {
      setProjectMembers([]);
      return;
    }
    try {
      const { data } = await apiListRequest(
        buildApiPath(`/projects/${workspaceData.selectedProjectId}/members`, { limit: 200 }),
        { token: auth.token }
      );
      setProjectMembers(data);
    } catch {
      setProjectMembers([]);
    }
  }, [apiEnabled, auth.token, workspaceData.selectedProjectId]);

  React.useEffect(() => {
    refreshProjectMembers();
  }, [refreshProjectMembers]);

  const selectedProjectMembership = React.useMemo(
    () => projectMembers.find((member) => member.user_id === auth.user?.user_id) || null,
    [auth.user, projectMembers]
  );
  const selectedProjectRole = selectedProjectMembership?.role || "";
  const canContributeToProject =
    auth.user?.role === "admin" ||
    selectedProjectRole === "contributor" ||
    selectedProjectRole === "owner";
  const canManageProjectMembers = auth.user?.role === "admin" || selectedProjectRole === "owner";

  async function handleAddProjectMember(event) {
    event.preventDefault();
    if (!canManageProjectMembers || !workspaceData.selectedProjectId || !memberUsername.trim()) {
      return;
    }
    setBusy(true);
    setFlash("", "");
    try {
      await apiRequest(`/projects/${workspaceData.selectedProjectId}/members`, {
        body: { username: memberUsername.trim(), role: memberRole },
        method: "POST",
        token: auth.token,
      });
      setMemberUsername("");
      await refreshProjectMembers();
      setFlash("Project member updated.");
    } catch (err) {
      setFlash("", err.message || "Failed to update project member.");
    } finally {
      setBusy(false);
    }
  }

  async function handleUpdateProjectMember(userId, role) {
    if (!canManageProjectMembers || !workspaceData.selectedProjectId) {
      return;
    }
    setBusy(true);
    setFlash("", "");
    try {
      await apiRequest(`/projects/${workspaceData.selectedProjectId}/members/${userId}`, {
        body: { role },
        method: "PATCH",
        token: auth.token,
      });
      await refreshProjectMembers();
      setFlash("Project member updated.");
    } catch (err) {
      setFlash("", err.message || "Failed to update project member.");
    } finally {
      setBusy(false);
    }
  }

  async function handleRemoveProjectMember(userId) {
    if (!canManageProjectMembers || !workspaceData.selectedProjectId) {
      return;
    }
    setBusy(true);
    setFlash("", "");
    try {
      await apiRequest(`/projects/${workspaceData.selectedProjectId}/members/${userId}`, {
        method: "DELETE",
        token: auth.token,
      });
      await refreshProjectMembers();
      setFlash("Project member removed.");
    } catch (err) {
      setFlash("", err.message || "Failed to remove project member.");
    } finally {
      setBusy(false);
    }
  }
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
    canWrite: canContributeToProject,
    selectedProjectId: workspaceData.selectedProjectId,
    refreshProjectData: workspaceData.refreshProjectData,
    setBusy,
    setFlash,
    questionText: workspaceForms.questionText,
    setQuestionText: workspaceForms.setQuestionText,
    questionType: workspaceForms.questionType,
    questionHypothesis: workspaceForms.questionHypothesis,
    setQuestionHypothesis: workspaceForms.setQuestionHypothesis,
    questionParentIds: workspaceForms.questionParentIds,
    setQuestionParentIds: workspaceForms.setQuestionParentIds,
  });
  const noteActions = useNoteActions({
    token: auth.token,
    canWrite: canContributeToProject,
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
    canWrite: canContributeToProject,
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
    canWrite: canContributeToProject,
    selectedProjectId: workspaceData.selectedProjectId,
    questions: workspaceData.questions,
    datasets: workspaceData.stagedDatasets,
    refreshProjectData: workspaceData.refreshProjectData,
    setBusy,
    setFlash,
  });
  const analysis = useAnalysisWorkflow({
    enabled: isHomeRoute && apiEnabled,
    token: auth.token,
    canWrite: canContributeToProject,
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
    onOpenGraph: () => navigate("/app/graph"),
    onOpenBatches: () => navigate("/app/batches"),
    projectMembers,
    canManageProjectMembers,
    memberUsername,
    memberRole,
    onMemberUsernameChange: (event) => setMemberUsername(event.target.value),
    onMemberRoleChange: (event) => setMemberRole(event.target.value),
    onAddProjectMember: handleAddProjectMember,
    onUpdateProjectMember: handleUpdateProjectMember,
    onRemoveProjectMember: handleRemoveProjectMember,
  };

  const isCaptureRoute = route.kind === "capture";

  return (
    <div className={`app-shell${isCaptureRoute ? " capture-app-shell" : ""}`}>
      <AppHeader
        activeKind={route.kind}
        authEnabled={auth.authEnabled}
        navigate={navigate}
        user={auth.user}
        onLogout={auth.handleLogout}
      />

      <FlashMessages message={message} error={error} />
      <PendingUploadsBadge />
      <PendingBatchBanner enabled={apiEnabled} token={auth.token} navigate={navigate} />

      {!auth.authChecked ? (
        <section className="grid">
          <WorkflowCoverageCard />
        </section>
      ) : route.kind === "enroll" ? (
        // Pairing happens before the phone has a token; bypass the login form
        // and the workspace shell, render the enroll page directly.
        <section className="grid">
          <EnrollPage replace={replace} setFlash={setFlash} />
        </section>
      ) : auth.authEnabled && !auth.token ? (
        <section className="grid">
          <AuthForm
            authBootstrapStatus={auth.authBootstrapStatus}
            authBootstrapToken={auth.authBootstrapToken}
            authInviteEmail={auth.authInviteEmail}
            authInviteToken={auth.authInviteToken}
            authMode={auth.authMode}
            authUsername={auth.authUsername}
            authPassword={auth.authPassword}
            authBusy={auth.authBusy}
            onBootstrapTokenChange={(event) => auth.setAuthBootstrapToken(event.target.value)}
            onSubmit={auth.handleAuthSubmit}
            onUsernameChange={(event) => auth.setAuthUsername(event.target.value)}
            onPasswordChange={(event) => auth.setAuthPassword(event.target.value)}
            onToggleMode={() =>
              auth.setAuthMode((current) =>
                current === "login" ? "register" : "login"
              )
            }
          />
          <WorkflowCoverageCard />
        </section>
      ) : (
        <section className="grid">
          {isCaptureRoute ? (
            <MobileCaptureCard
              token={auth.token}
              canWrite={canContributeToProject}
              projects={workspaceData.projects}
              selectedProjectId={workspaceData.selectedProjectId}
              onSelectedProjectChange={workspaceData.setSelectedProjectId}
              questions={workspaceData.questions}
              datasets={workspaceData.datasets}
              sessions={sessionData.sessions}
              navigate={navigate}
              setBusy={setBusy}
              setFlash={setFlash}
              refreshProjectCounts={workspaceData.refreshProjectCounts}
              refreshRecentNotes={noteData.refreshRecentNotes}
            />
          ) : null}

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
              projectMembers={projectMembers}
              projectAccess={{
                canContribute: canContributeToProject,
                canManageMembers: canManageProjectMembers,
                memberRole,
                memberUsername,
                onAddMember: handleAddProjectMember,
                onMemberRoleChange: (event) => setMemberRole(event.target.value),
                onMemberUsernameChange: (event) => setMemberUsername(event.target.value),
                onRemoveMember: handleRemoveProjectMember,
                onUpdateMember: handleUpdateProjectMember,
                role: selectedProjectRole,
              }}
            />
          ) : (
            <Dashboard {...dashboardProps} />
          )}

          {route.kind === "devices" ? (
            <DevicesPage
              token={auth.token}
              canWrite={Boolean(auth.user)}
              navigate={navigate}
              setFlash={setFlash}
            />
          ) : null}

          {route.kind === "users" ? (
            <UsersPage
              token={auth.token}
              canManageUsers={auth.user?.role === "admin"}
              setBusy={setBusy}
              setFlash={setFlash}
            />
          ) : null}

          {route.kind === "graph" ? (
            <ProjectGraphExplorer
              token={auth.token}
              projects={workspaceData.projects}
              selectedProjectId={workspaceData.selectedProjectId}
              onSelectedProjectChange={(event) =>
                workspaceData.setSelectedProjectId(event.target.value)
              }
              navigate={navigate}
              setFlash={setFlash}
            />
          ) : null}

          {route.kind === "batches" ? (
            <BatchReviewPage
              token={auth.token}
              projects={workspaceData.projects}
              selectedProjectId={workspaceData.selectedProjectId}
              onSelectedProjectChange={(event) =>
                workspaceData.setSelectedProjectId(event.target.value)
              }
              navigate={navigate}
              canManageGraph={canManageProjectMembers}
              setBusy={setBusy}
              setFlash={setFlash}
            />
          ) : null}

          {route.kind === "question" ? (
            <QuestionDetailCard
              token={auth.token}
              questionId={route.questionId}
              projects={workspaceData.projects}
              questions={workspaceData.questions}
              navigate={navigate}
              onSetActiveProject={workspaceData.setSelectedProjectId}
              canWrite={canContributeToProject}
              setBusy={setBusy}
              setFlash={setFlash}
            />
          ) : null}

          {route.kind === "note" ? (
            <NoteDetailCard
              token={auth.token}
              noteId={route.noteId}
              projects={workspaceData.projects}
              navigate={navigate}
              onSetActiveProject={workspaceData.setSelectedProjectId}
              canWrite={canContributeToProject}
              setBusy={setBusy}
              setFlash={setFlash}
            />
          ) : null}

          {route.kind === "graph-draft" ? (
            <GraphDraftDetailCard
              token={auth.token}
              changeSetId={route.changeSetId}
              navigate={navigate}
              canWrite={canContributeToProject}
              canManageGraph={canManageProjectMembers}
              user={auth.user}
              setBusy={setBusy}
              setFlash={setFlash}
            />
          ) : null}

          {route.kind === "batch" ? (
            <GraphDraftDetailCard
              token={auth.token}
              changeSetId={route.changeSetId}
              navigate={navigate}
              canWrite={canContributeToProject}
              canManageGraph={canManageProjectMembers}
              user={auth.user}
              setBusy={setBusy}
              setFlash={setFlash}
              backPath="/app/batches"
            />
          ) : null}

          {route.kind === "session" ? (
            <SessionDetailCard
              token={auth.token}
              sessionId={route.sessionId}
              projects={workspaceData.projects}
              navigate={navigate}
              onSetActiveProject={workspaceData.setSelectedProjectId}
              canWrite={canContributeToProject}
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

          {route.kind === "goal" ? (
            <GoalDetailCard token={auth.token} goalId={route.goalId} navigate={navigate} />
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
