import * as React from "react";

import { AgentAccessPage } from "./features/agent-access.jsx";
import { Dashboard } from "./features/dashboard-projects.jsx";
import { BatchReviewPage, PendingBatchBanner } from "./features/batches.jsx";
import { DevicesPage } from "./features/devices.jsx";
import { EnrollPage } from "./features/enroll.jsx";
import { ExperimentDetailCard } from "./features/experiments/index.js";
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

/** Spoken name for each route, used to announce client-side navigation. */
const ROUTE_LABELS = {
  agents: "Agents",
  batch: "Review batch",
  batches: "Review batches",
  capture: "Capture",
  dataset: "Dataset",
  devices: "Devices",
  enroll: "Pair device",
  experiment: "Experiment",
  goal: "Goal",
  graph: "Project graph",
  "graph-draft": "Graph draft",
  home: "Home",
  note: "Note",
  question: "Question",
  session: "Session",
  unknown: "Page not found",
  users: "Users",
  visualization: "Visualization",
};

function routeLabel(kind) {
  return ROUTE_LABELS[kind] || "Page";
}

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
  const isFocusedReviewRoute = route.kind === "batch";

  // A client-side route change swaps the view without the browser's usual focus
  // reset, so a keyboard or screen-reader user is left where they were with no
  // signal that anything moved. Put focus on the new main landmark and say
  // where they have landed.
  const mainRef = React.useRef(null);
  const [routeAnnouncement, setRouteAnnouncement] = React.useState("");
  const announcedKindRef = React.useRef(null);
  React.useEffect(() => {
    // Only on a real view change; a re-render within a view must never steal
    // focus from the control someone is currently using.
    if (announcedKindRef.current === route.kind) {
      return;
    }
    const isFirstRender = announcedKindRef.current === null;
    announcedKindRef.current = route.kind;
    if (isFirstRender) {
      // A fresh document load already starts the user at the top.
      return;
    }
    setRouteAnnouncement(routeLabel(route.kind));
    mainRef.current?.focus();
  }, [route.kind]);

  return (
    <div
      className={`app-shell${isCaptureRoute ? " capture-app-shell" : ""}${
        isFocusedReviewRoute ? " review-app-shell" : ""
      }`}
    >
      <a className="skip-link" href="#main-content">
        Skip to main content
      </a>
      {/* A bare polite live region rather than role="status": the app already
          uses status elements for transient notices, and a second global one
          would make "the status message" ambiguous for both assistive tech and
          tests. aria-atomic so the whole view name is read, not a diff. */}
      <p aria-atomic="true" aria-live="polite" className="sr-only" data-testid="route-announcer">
        {routeAnnouncement}
      </p>
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

      {/* Real landmark for the skip link, and the focus target on route
          change. tabIndex -1 makes it programmatically focusable without
          adding it to the tab order. */}
      <main id="main-content" ref={mainRef} tabIndex={-1}>
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
            ) : route.kind === "graph" || isFocusedReviewRoute ? null : (
              // The graph explorer has its own project picker and fills the
              // viewport; stacking the Dashboard card (second picker, New
              // Project + member forms) next to it just buries the canvas.
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

            {route.kind === "agents" ? (
              <AgentAccessPage
                token={auth.token}
                user={auth.user}
                authEnabled={auth.authEnabled}
                navigate={navigate}
                setBusy={setBusy}
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
                canManageGraph={canContributeToProject}
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

            {route.kind === "experiment" ? (
              <ExperimentDetailCard
                token={auth.token}
                experimentId={route.experimentId}
                projects={workspaceData.projects}
                navigate={navigate}
                onSetActiveProject={workspaceData.setSelectedProjectId}
                canWrite={canContributeToProject}
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
      </main>

      {busy ? <p className="subtle">Syncing...</p> : null}
    </div>
  );
}

export { App };
