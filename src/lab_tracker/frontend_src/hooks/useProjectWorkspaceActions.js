import { apiRequest } from "../shared/api.js";

function useProjectWorkspaceActions({
  token,
  canWrite,
  selectedProjectId,
  refreshProjects,
  refreshProjectData,
  setBusy,
  setFlash,
  setSelectedProjectId,
  setSessions,
  projectName,
  setProjectName,
  projectDescription,
  setProjectDescription,
  questionText,
  setQuestionText,
  questionType,
  questionHypothesis,
  setQuestionHypothesis,
  noteText,
  setNoteText,
  uploadFile,
  setUploadFile,
  uploadTranscript,
  setUploadTranscript,
  uploadTargetQuestionId,
  setUploadTargetQuestionId,
  sessionType,
  sessionPrimaryQuestionId,
}) {
  async function handleCreateProject(event) {
    event.preventDefault();
    if (!canWrite) {
      return;
    }
    if (!projectName.trim()) {
      setFlash("", "Project name is required.");
      return;
    }

    setBusy(true);
    setFlash("", "");
    try {
      const created = await apiRequest("/projects", {
        body: {
          description: projectDescription.trim() || null,
          name: projectName.trim(),
        },
        method: "POST",
        token,
      });
      setProjectName("");
      setProjectDescription("");
      await refreshProjects();
      setSelectedProjectId(created.project_id);
      setFlash("Project created.");
    } catch (err) {
      setFlash("", err.message || "Failed to create project.");
    } finally {
      setBusy(false);
    }
  }

  async function handleCreateQuestion(event) {
    event.preventDefault();
    if (!selectedProjectId || !canWrite) {
      return;
    }
    if (!questionText.trim()) {
      setFlash("", "Question text is required.");
      return;
    }

    setBusy(true);
    setFlash("", "");
    try {
      await apiRequest("/questions", {
        body: {
          hypothesis: questionHypothesis.trim() || null,
          project_id: selectedProjectId,
          question_type: questionType,
          text: questionText.trim(),
        },
        method: "POST",
        token,
      });
      setQuestionText("");
      setQuestionHypothesis("");
      await refreshProjectData(selectedProjectId);
      setFlash("Question staged.");
    } catch (err) {
      setFlash("", err.message || "Failed to create question.");
    } finally {
      setBusy(false);
    }
  }

  async function handleActivateQuestion(questionId) {
    if (!canWrite) {
      return;
    }
    setBusy(true);
    setFlash("", "");
    try {
      await apiRequest(`/questions/${questionId}`, {
        body: { status: "active" },
        method: "PATCH",
        token,
      });
      await refreshProjectData(selectedProjectId);
      setFlash("Question activated.");
    } catch (err) {
      setFlash("", err.message || "Failed to activate question.");
    } finally {
      setBusy(false);
    }
  }

  async function handleCreateTextNote(event) {
    event.preventDefault();
    if (!selectedProjectId || !canWrite) {
      return;
    }
    if (!noteText.trim()) {
      setFlash("", "Note text is required.");
      return;
    }

    setBusy(true);
    setFlash("", "");
    try {
      await apiRequest("/notes", {
        body: {
          project_id: selectedProjectId,
          raw_content: noteText.trim(),
        },
        method: "POST",
        token,
      });
      setNoteText("");
      await refreshProjectData(selectedProjectId);
      setFlash("Text note added.");
    } catch (err) {
      setFlash("", err.message || "Failed to create note.");
    } finally {
      setBusy(false);
    }
  }

  async function handleUploadNote(event) {
    event.preventDefault();
    if (!selectedProjectId || !canWrite) {
      return;
    }
    if (!uploadFile) {
      setFlash("", "Select an image or note file before upload.");
      return;
    }

    setBusy(true);
    setFlash("", "");
    try {
      const payload = new FormData();
      payload.append("file", uploadFile);
      payload.append("project_id", selectedProjectId);
      if (uploadTranscript.trim()) {
        payload.append("transcribed_text", uploadTranscript.trim());
      }
      if (uploadTargetQuestionId) {
        payload.append(
          "targets",
          JSON.stringify([
            {
              entity_id: uploadTargetQuestionId,
              entity_type: "question",
            },
          ])
        );
      }

      await apiRequest("/notes/upload-file", {
        body: payload,
        method: "POST",
        token,
      });
      setUploadFile(null);
      setUploadTranscript("");
      setUploadTargetQuestionId("");
      event.target.reset();
      await refreshProjectData(selectedProjectId);
      setFlash("Photo note uploaded.");
    } catch (err) {
      setFlash("", err.message || "Failed to upload note.");
    } finally {
      setBusy(false);
    }
  }

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
      await refreshProjectData(selectedProjectId);
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
        await refreshProjectData(selectedProjectId);
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
    handleActivateQuestion,
    handleCloseSession,
    handleCreateProject,
    handleCreateQuestion,
    handleCreateSession,
    handleCreateTextNote,
    handlePromoteSession,
    handleUploadNote,
  };
}

export { useProjectWorkspaceActions };
