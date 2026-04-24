import { apiRequest } from "../shared/api.js";

function useSessionActions({
  token,
  canWrite,
  selectedProjectId,
  refreshActiveSessions,
  setBusy,
  setFlash,
  setSessions,
  sessionType,
  sessionPrimaryQuestionId,
}) {
  async function handleCreateSession(event) {
    event.preventDefault();
    if (!selectedProjectId || !canWrite) {
      return;
    }
    if (sessionType === "scientific" && !sessionPrimaryQuestionId) {
      setFlash("", "Pick a primary question for the scientific session.");
      return;
    }

    setBusy(true);
    setFlash("", "");
    try {
      const body = {
        project_id: selectedProjectId,
        session_type: sessionType,
      };
      if (sessionType === "scientific") {
        body.primary_question_id = sessionPrimaryQuestionId;
      }
      await apiRequest("/sessions", {
        body,
        method: "POST",
        token,
      });
      await refreshActiveSessions(selectedProjectId);
      setFlash("Session started.");
    } catch (err) {
      setFlash("", err.message || "Failed to start session.");
    } finally {
      setBusy(false);
    }
  }

  async function handleCloseSession(sessionId, projectId = "") {
    if (!canWrite) {
      return null;
    }
    setBusy(true);
    setFlash("", "");
    try {
      const payload = await apiRequest(`/sessions/${sessionId}`, {
        body: { ended_at: new Date().toISOString(), status: "closed" },
        method: "PATCH",
        token,
      });
      if (projectId && projectId === selectedProjectId) {
        await refreshActiveSessions(selectedProjectId);
      }
      setFlash("Session closed.");
      return payload;
    } catch (err) {
      setFlash("", err.message || "Failed to close session.");
      return null;
    } finally {
      setBusy(false);
    }
  }

  async function handlePromoteSession(sessionId, primaryQuestionId, projectId = "") {
    if (!canWrite) {
      return null;
    }
    if (!primaryQuestionId) {
      setFlash("", "Pick a primary question to promote the session.");
      return null;
    }

    setBusy(true);
    setFlash("", "");
    try {
      const payload = await apiRequest(`/sessions/${sessionId}/promote`, {
        body: { primary_question_id: primaryQuestionId },
        method: "POST",
        token,
      });
      if (projectId && projectId === selectedProjectId) {
        setSessions((current) =>
          current.map((item) => (item.session_id === payload.session_id ? payload : item))
        );
      }
      setFlash("Session promoted.");
      return payload;
    } catch (err) {
      setFlash("", err.message || "Failed to promote session.");
      return null;
    } finally {
      setBusy(false);
    }
  }

  return {
    handleCloseSession,
    handleCreateSession,
    handlePromoteSession,
  };
}

export { useSessionActions };
