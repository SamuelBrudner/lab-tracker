import * as React from "react";

import { apiRequest, buildApiPath, fetchAllPages } from "../shared/api.js";

const { useCallback, useEffect, useMemo, useState } = React;

function useProjectWorkspace({ token, canWrite, setBusy, setFlash }) {
  const [projects, setProjects] = useState([]);
  const [selectedProjectId, setSelectedProjectId] = useState("");
  const [questions, setQuestions] = useState([]);
  const [datasets, setDatasets] = useState([]);
  const [notes, setNotes] = useState([]);
  const [sessions, setSessions] = useState([]);

  const [projectName, setProjectName] = useState("");
  const [projectDescription, setProjectDescription] = useState("");

  const [questionText, setQuestionText] = useState("");
  const [questionType, setQuestionType] = useState("descriptive");
  const [questionHypothesis, setQuestionHypothesis] = useState("");

  const [noteText, setNoteText] = useState("");
  const [uploadFile, setUploadFile] = useState(null);
  const [uploadTranscript, setUploadTranscript] = useState("");
  const [uploadTargetQuestionId, setUploadTargetQuestionId] = useState("");

  const [sessionType, setSessionType] = useState("scientific");
  const [sessionPrimaryQuestionId, setSessionPrimaryQuestionId] = useState("");

  const stagedQuestions = useMemo(
    () => questions.filter((item) => item.status === "staged"),
    [questions]
  );
  const activeQuestions = useMemo(
    () => questions.filter((item) => item.status === "active"),
    [questions]
  );
  const selectedProject = useMemo(
    () => projects.find((item) => item.project_id === selectedProjectId) || null,
    [projects, selectedProjectId]
  );

  const clearProjectState = useCallback(() => {
    setSelectedProjectId("");
    setQuestions([]);
    setDatasets([]);
    setNotes([]);
    setSessions([]);
  }, []);

  const refreshProjectData = useCallback(
    async (projectId) => {
      if (!projectId || !token) {
        return;
      }

      const [nextQuestions, nextDatasets, nextNotes, nextSessions] = await Promise.all([
        fetchAllPages(buildApiPath("/questions", { project_id: projectId }), { token }),
        fetchAllPages(buildApiPath("/datasets", { project_id: projectId }), { token }),
        fetchAllPages(buildApiPath("/notes", { project_id: projectId }), { token }),
        fetchAllPages(buildApiPath("/sessions", { project_id: projectId }), { token }),
      ]);

      setQuestions(nextQuestions);
      setDatasets(nextDatasets);
      setNotes(nextNotes);
      setSessions(nextSessions);
    },
    [token]
  );

  const refreshProjects = useCallback(async () => {
    if (!token) {
      return [];
    }
    const nextProjects = await fetchAllPages("/projects", { token });
    setProjects(nextProjects);
    if (nextProjects.length === 0) {
      clearProjectState();
      return nextProjects;
    }
    setSelectedProjectId((current) => {
      if (current && nextProjects.some((item) => item.project_id === current)) {
        return current;
      }
      return nextProjects[0].project_id;
    });
    return nextProjects;
  }, [clearProjectState, token]);

  useEffect(() => {
    if (!token) {
      setProjects([]);
      clearProjectState();
      return;
    }

    let canceled = false;
    setBusy(true);
    refreshProjects()
      .catch((err) => {
        if (!canceled) {
          setFlash("", err.message || "Failed to load projects.");
        }
      })
      .finally(() => {
        if (!canceled) {
          setBusy(false);
        }
      });

    return () => {
      canceled = true;
    };
  }, [clearProjectState, refreshProjects, setBusy, setFlash, token]);

  useEffect(() => {
    if (!token || !selectedProjectId) {
      return;
    }

    let canceled = false;
    setBusy(true);
    refreshProjectData(selectedProjectId)
      .catch((err) => {
        if (!canceled) {
          setFlash("", err.message || "Unable to load project data.");
        }
      })
      .finally(() => {
        if (!canceled) {
          setBusy(false);
        }
      });

    return () => {
      canceled = true;
    };
  }, [refreshProjectData, selectedProjectId, setBusy, setFlash, token]);

  useEffect(() => {
    if (sessionType !== "scientific") {
      return;
    }
    const nextActiveQuestions = questions.filter((item) => item.status === "active");
    if (nextActiveQuestions.length === 0) {
      if (!questions.some((item) => item.question_id === sessionPrimaryQuestionId)) {
        setSessionPrimaryQuestionId("");
      }
      return;
    }
    const hasCurrent = nextActiveQuestions.some(
      (item) => item.question_id === sessionPrimaryQuestionId
    );
    if (!hasCurrent) {
      setSessionPrimaryQuestionId(nextActiveQuestions[0].question_id);
    }
  }, [questions, sessionPrimaryQuestionId, sessionType]);

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
    activeQuestions,
    datasetPrimaryQuestionOptions: questions,
    datasets,
    handleActivateQuestion,
    handleCloseSession,
    handleCreateProject,
    handleCreateQuestion,
    handleCreateSession,
    handleCreateTextNote,
    handlePromoteSession,
    handleUploadNote,
    noteText,
    notes,
    projectDescription,
    projectName,
    projects,
    questionHypothesis,
    questionText,
    questionType,
    questions,
    refreshProjectData,
    selectedProject,
    selectedProjectId,
    sessionPrimaryQuestionId,
    sessionType,
    sessions,
    setNoteText,
    setProjectDescription,
    setProjectName,
    setQuestionHypothesis,
    setQuestionText,
    setQuestionType,
    setSelectedProjectId,
    setSessionPrimaryQuestionId,
    setSessionType,
    setUploadFile,
    setUploadTargetQuestionId,
    setUploadTranscript,
    stagedQuestions,
    uploadTargetQuestionId,
    uploadTranscript,
  };
}

export { useProjectWorkspace };
