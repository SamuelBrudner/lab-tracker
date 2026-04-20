import * as React from "react";

import { apiRequest, toBase64Content } from "../shared/api.js";
import { TOKEN_STORAGE_KEY } from "../shared/constants.js";
import { useAppRoute } from "../shared/routing.jsx";

const { useCallback, useEffect, useMemo, useState } = React;

function useLabTrackerAppState() {
  const { navigate, replace, route } = useAppRoute();

  const [token, setToken] = useState(() => localStorage.getItem(TOKEN_STORAGE_KEY) || "");
  const [user, setUser] = useState(null);

  const [authMode, setAuthMode] = useState("login");
  const [authUsername, setAuthUsername] = useState("");
  const [authPassword, setAuthPassword] = useState("");
  const [authBusy, setAuthBusy] = useState(false);

  const [projects, setProjects] = useState([]);
  const [selectedProjectId, setSelectedProjectId] = useState("");
  const [questions, setQuestions] = useState([]);
  const [datasets, setDatasets] = useState([]);
  const [notes, setNotes] = useState([]);
  const [sessions, setSessions] = useState([]);
  const [analyses, setAnalyses] = useState([]);
  const [visualizations, setVisualizations] = useState([]);

  const [extractionNoteId, setExtractionNoteId] = useState("");
  const [extractionNote, setExtractionNote] = useState(null);
  const [extractionNoteRaw, setExtractionNoteRaw] = useState(null);
  const [extractionNoteRawError, setExtractionNoteRawError] = useState("");
  const [extractionCandidates, setExtractionCandidates] = useState([]);

  const [projectName, setProjectName] = useState("");
  const [projectDescription, setProjectDescription] = useState("");

  const [questionText, setQuestionText] = useState("");
  const [questionType, setQuestionType] = useState("descriptive");
  const [questionHypothesis, setQuestionHypothesis] = useState("");

  const [noteText, setNoteText] = useState("");

  const [uploadFile, setUploadFile] = useState(null);
  const [uploadTranscript, setUploadTranscript] = useState("");
  const [uploadTargetQuestionId, setUploadTargetQuestionId] = useState("");

  const [datasetPrimaryQuestionId, setDatasetPrimaryQuestionId] = useState("");
  const [datasetSecondaryRaw, setDatasetSecondaryRaw] = useState("");
  const [datasetFilesById, setDatasetFilesById] = useState({});
  const [datasetReviewsById, setDatasetReviewsById] = useState({});

  const [sessionType, setSessionType] = useState("scientific");
  const [sessionPrimaryQuestionId, setSessionPrimaryQuestionId] = useState("");

  const [analysisDatasetIds, setAnalysisDatasetIds] = useState([]);
  const [analysisCodeVersion, setAnalysisCodeVersion] = useState("");
  const [analysisMethodHash, setAnalysisMethodHash] = useState("");
  const [analysisEnvironmentHash, setAnalysisEnvironmentHash] = useState("");

  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");

  const canWrite = Boolean(user && (user.role === "admin" || user.role === "editor"));

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

  function setFlash(nextMessage, nextError = "") {
    setMessage(nextMessage);
    setError(nextError);
  }

  const refreshProjectData = useCallback(
    async (projectId) => {
      if (!projectId || !token) {
        return;
      }

      const encodedProjectId = encodeURIComponent(projectId);
      const [
        nextQuestions,
        nextDatasets,
        nextNotes,
        nextSessions,
        nextAnalyses,
        nextVisualizations,
      ] = await Promise.all([
        apiRequest(`/questions?project_id=${encodedProjectId}&limit=200`, { token }),
        apiRequest(`/datasets?project_id=${encodedProjectId}&limit=200`, { token }),
        apiRequest(`/notes?project_id=${encodedProjectId}&limit=200`, { token }),
        apiRequest(`/sessions?project_id=${encodedProjectId}&limit=200`, { token }),
        apiRequest(`/analyses?project_id=${encodedProjectId}&limit=200`, { token }),
        apiRequest(`/visualizations?project_id=${encodedProjectId}&limit=200`, { token }),
      ]);

      setQuestions(nextQuestions);
      setDatasets(nextDatasets);
      setNotes(nextNotes);
      setSessions(nextSessions);
      setAnalyses(nextAnalyses);
      setVisualizations(nextVisualizations);

      if (!datasetPrimaryQuestionId && nextQuestions.length > 0) {
        setDatasetPrimaryQuestionId(nextQuestions[0].question_id);
      }

      if (sessionType === "scientific" && !sessionPrimaryQuestionId) {
        const nextActiveQuestions = nextQuestions.filter((item) => item.status === "active");
        if (nextActiveQuestions.length > 0) {
          setSessionPrimaryQuestionId(nextActiveQuestions[0].question_id);
        }
      }
    },
    [datasetPrimaryQuestionId, sessionPrimaryQuestionId, sessionType, token]
  );

  const bootstrapSession = useCallback(
    async (nextToken) => {
      const [nextUser, nextProjects] = await Promise.all([
        apiRequest("/auth/me", { token: nextToken }),
        apiRequest("/projects", { token: nextToken }),
      ]);
      setUser(nextUser);
      setProjects(nextProjects);
      if (nextProjects.length > 0) {
        setSelectedProjectId((current) => {
          if (current && nextProjects.some((item) => item.project_id === current)) {
            return current;
          }
          return nextProjects[0].project_id;
        });
      } else {
        setSelectedProjectId("");
        setQuestions([]);
        setDatasets([]);
        setNotes([]);
        setSessions([]);
        setAnalyses([]);
        setVisualizations([]);
      }
    },
    []
  );

  const refreshProjects = useCallback(async () => {
    if (!token) {
      return;
    }
    const nextProjects = await apiRequest("/projects", { token });
    setProjects(nextProjects);
    if (nextProjects.length === 0) {
      setSelectedProjectId("");
      setQuestions([]);
      setDatasets([]);
      setNotes([]);
      setSessions([]);
      setAnalyses([]);
      setVisualizations([]);
      return;
    }
    setSelectedProjectId((current) => {
      if (current && nextProjects.some((item) => item.project_id === current)) {
        return current;
      }
      return nextProjects[0].project_id;
    });
  }, [token]);

  const refreshActiveProject = useCallback(async () => {
    if (!selectedProjectId || !token) {
      return { ok: true };
    }
    try {
      await refreshProjectData(selectedProjectId);
      return { ok: true };
    } catch (err) {
      return {
        error: err.message || "Failed to refresh active project.",
        ok: false,
      };
    }
  }, [refreshProjectData, selectedProjectId, token]);

  const loadDatasetFiles = useCallback(
    async (datasetId) => {
      if (!token) {
        return null;
      }

      setDatasetFilesById((current) => ({
        ...current,
        [datasetId]: {
          error: "",
          items: current[datasetId]?.items || [],
          loaded: current[datasetId]?.loaded || false,
          loading: true,
        },
      }));

      try {
        const items = await apiRequest(`/datasets/${datasetId}/files?limit=200`, { token });
        const normalized = Array.isArray(items) ? items : [];
        setDatasetFilesById((current) => ({
          ...current,
          [datasetId]: {
            error: "",
            items: normalized,
            loaded: true,
            loading: false,
          },
        }));
        return normalized;
      } catch (err) {
        setDatasetFilesById((current) => ({
          ...current,
          [datasetId]: {
            error: err.message || "Failed to load dataset files.",
            items: current[datasetId]?.items || [],
            loaded: true,
            loading: false,
          },
        }));
        return null;
      }
    },
    [token]
  );

  useEffect(() => {
    if (token) {
      localStorage.setItem(TOKEN_STORAGE_KEY, token);
    } else {
      localStorage.removeItem(TOKEN_STORAGE_KEY);
    }
  }, [token]);

  useEffect(() => {
    setDatasetFilesById({});
    setDatasetReviewsById({});
    setAnalysisDatasetIds([]);
    setSessionPrimaryQuestionId("");
  }, [selectedProjectId, token]);

  useEffect(() => {
    setExtractionNoteId("");
    setExtractionNote(null);
    setExtractionNoteRaw(null);
    setExtractionNoteRawError("");
    setExtractionCandidates([]);
  }, [selectedProjectId, token]);

  useEffect(() => {
    let canceled = false;

    if (!token) {
      setUser(null);
      setProjects([]);
      setSelectedProjectId("");
      setQuestions([]);
      setDatasets([]);
      setNotes([]);
      setSessions([]);
      setAnalyses([]);
      setVisualizations([]);
      return () => {
        canceled = true;
      };
    }

    setBusy(true);
    setFlash("", "");
    bootstrapSession(token)
      .catch((err) => {
        if (!canceled) {
          setToken("");
          setFlash("", err.message || "Failed to restore session.");
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
  }, [bootstrapSession, token]);

  useEffect(() => {
    let canceled = false;
    if (!token || !selectedProjectId) {
      return () => {
        canceled = true;
      };
    }

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
  }, [refreshProjectData, selectedProjectId, token]);

  useEffect(() => {
    let canceled = false;
    const reviewRequired = Boolean(
      selectedProject && selectedProject.review_policy && selectedProject.review_policy !== "none"
    );

    if (!token || !selectedProjectId || !reviewRequired) {
      setDatasetReviewsById({});
      return () => {
        canceled = true;
      };
    }

    const stagedDatasets = (datasets || []).filter((dataset) => dataset.status === "staged");
    const datasetIds = stagedDatasets.map((dataset) => dataset.dataset_id);

    if (datasetIds.length === 0) {
      setDatasetReviewsById({});
      return () => {
        canceled = true;
      };
    }

    setDatasetReviewsById((current) => {
      const next = {};
      datasetIds.forEach((datasetId) => {
        next[datasetId] = {
          error: "",
          loading: true,
          review: current[datasetId]?.review || null,
        };
      });
      return next;
    });

    Promise.all(
      datasetIds.map(async (datasetId) => {
        try {
          const review = await apiRequest(`/datasets/${datasetId}/review`, { token });
          return { datasetId, error: "", review };
        } catch (err) {
          if (err && err.status === 404) {
            return { datasetId, error: "", review: null };
          }
          return {
            datasetId,
            error: err.message || "Failed to load review.",
            review: null,
          };
        }
      })
    ).then((results) => {
      if (canceled) {
        return;
      }
      setDatasetReviewsById(() => {
        const next = {};
        results.forEach(({ datasetId, review, error: reviewError }) => {
          next[datasetId] = { error: reviewError, loading: false, review };
        });
        return next;
      });
    });

    return () => {
      canceled = true;
    };
  }, [datasets, selectedProject, selectedProjectId, token]);

  async function handleAuthSubmit(event) {
    event.preventDefault();
    if (!authUsername.trim() || !authPassword) {
      setFlash("", "Username and password are required.");
      return;
    }

    setAuthBusy(true);
    setFlash("", "");
    try {
      const endpoint = authMode === "register" ? "/auth/register" : "/auth/login";
      const payload = await apiRequest(endpoint, {
        body: {
          password: authPassword,
          username: authUsername.trim(),
        },
        method: "POST",
      });
      setToken(payload.access_token);
      setAuthPassword("");
      setFlash(
        authMode === "register"
          ? "Viewer account created. You are signed in."
          : "Signed in successfully."
      );
    } catch (err) {
      setFlash("", err.message || "Authentication failed.");
    } finally {
      setAuthBusy(false);
    }
  }

  function handleLogout() {
    setToken("");
    setUser(null);
    setAuthPassword("");
    replace("/app");
    setFlash("Signed out.", "");
  }

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
      const encoded = await toBase64Content(uploadFile);
      const payload = {
        content_base64: encoded,
        content_type: uploadFile.type || "application/octet-stream",
        filename: uploadFile.name,
        project_id: selectedProjectId,
        transcribed_text: uploadTranscript.trim() || null,
      };
      if (uploadTargetQuestionId) {
        payload.targets = [
          {
            entity_id: uploadTargetQuestionId,
            entity_type: "question",
          },
        ];
      }

      await apiRequest("/notes/upload", {
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

  function handleExtractionNoteIdChange(event) {
    const nextValue = event?.target?.value || "";
    setExtractionNoteId(nextValue);
    setExtractionNote(null);
    setExtractionNoteRaw(null);
    setExtractionNoteRawError("");
    setExtractionCandidates([]);
  }

  async function handleExtractQuestionCandidates(event) {
    event.preventDefault();
    if (!token || !canWrite || !selectedProjectId || !extractionNoteId) {
      return;
    }

    setBusy(true);
    setFlash("", "");
    try {
      const loadedNote = await apiRequest(`/notes/${extractionNoteId}`, { token });
      setExtractionNote(loadedNote);

      if (loadedNote.raw_asset) {
        try {
          const rawPayload = await apiRequest(`/notes/${extractionNoteId}/raw`, { token });
          setExtractionNoteRaw(rawPayload);
          setExtractionNoteRawError("");
        } catch (err) {
          setExtractionNoteRaw(null);
          setExtractionNoteRawError(err.message || "Failed to load raw preview.");
        }
      } else {
        setExtractionNoteRaw(null);
        setExtractionNoteRawError("");
      }

      const payload = await apiRequest(`/notes/${extractionNoteId}/extract-questions`, {
        body: {},
        method: "POST",
        token,
      });
      const extracted = Array.isArray(payload) ? payload : [];
      const batchId = Date.now().toString(36);
      setExtractionCandidates(
        extracted.map((item, index) => ({
          confidence: typeof item.confidence === "number" ? item.confidence : null,
          error: "",
          hypothesis: "",
          local_id: `${batchId}-${index}`,
          parent_question_ids: [],
          provenance: item.provenance || "",
          question_type: item.suggested_question_type || "other",
          selected: true,
          staged_question_id: "",
          status: "pending",
          text: String(item.text || ""),
        }))
      );
      setFlash(
        extracted.length === 0
          ? "No question candidates found for that note."
          : `Loaded ${extracted.length} question candidate(s).`
      );
    } catch (err) {
      setExtractionCandidates([]);
      setFlash("", err.message || "Failed to extract question candidates.");
    } finally {
      setBusy(false);
    }
  }

  function handleUpdateExtractionCandidate(localId, updates) {
    setExtractionCandidates((current) =>
      current.map((item) => (item.local_id === localId ? { ...item, ...updates } : item))
    );
  }

  function handleToggleExtractionCandidateSelected(localId) {
    setExtractionCandidates((current) =>
      current.map((item) =>
        item.local_id === localId ? { ...item, selected: !item.selected } : item
      )
    );
  }

  function handleSelectAllPendingCandidates() {
    setExtractionCandidates((current) =>
      current.map((item) => (item.status === "pending" ? { ...item, selected: true } : item))
    );
  }

  function handleClearCandidateSelection() {
    setExtractionCandidates((current) => current.map((item) => ({ ...item, selected: false })));
  }

  function handleRejectExtractionCandidates(candidateIds) {
    const resolvedIds = Array.isArray(candidateIds)
      ? candidateIds
      : extractionCandidates
          .filter((item) => item.selected && item.status === "pending")
          .map((item) => item.local_id);
    if (resolvedIds.length === 0) {
      return;
    }

    const rejectSet = new Set(resolvedIds);
    setExtractionCandidates((current) =>
      current.map((item) => {
        if (!rejectSet.has(item.local_id) || item.status !== "pending") {
          return item;
        }
        return { ...item, error: "", selected: false, status: "rejected" };
      })
    );
  }

  async function handleStageExtractionCandidates(candidateIds) {
    if (!token || !canWrite || !selectedProjectId || !extractionNote) {
      return;
    }

    const resolvedIds = Array.isArray(candidateIds)
      ? candidateIds
      : extractionCandidates
          .filter((item) => item.selected && item.status === "pending")
          .map((item) => item.local_id);
    if (resolvedIds.length === 0) {
      setFlash("", "Select at least one pending candidate to stage.");
      return;
    }

    const toStage = extractionCandidates.filter(
      (item) => resolvedIds.includes(item.local_id) && item.status === "pending"
    );
    if (toStage.length === 0) {
      setFlash("", "No pending candidates selected.");
      return;
    }

    const emptyTextIds = toStage
      .filter((item) => !String(item.text || "").trim())
      .map((item) => item.local_id);
    if (emptyTextIds.length > 0) {
      const emptySet = new Set(emptyTextIds);
      setExtractionCandidates((current) =>
        current.map((item) =>
          emptySet.has(item.local_id)
            ? { ...item, error: "Question text is required.", status: "error" }
            : item
        )
      );
      setFlash("", "One or more candidates are missing question text.");
      return;
    }

    setBusy(true);
    setFlash("", "");
    const updatesById = new Map();
    const createdQuestionIds = [];

    try {
      const results = await Promise.allSettled(
        toStage.map((candidate) =>
          apiRequest("/questions", {
            body: {
              created_by: candidate.provenance || null,
              created_from: "meeting_capture",
              hypothesis: candidate.hypothesis.trim() || null,
              parent_question_ids:
                candidate.parent_question_ids && candidate.parent_question_ids.length > 0
                  ? candidate.parent_question_ids
                  : null,
              project_id: selectedProjectId,
              question_type: candidate.question_type || "other",
              status: "staged",
              text: String(candidate.text || "").trim(),
            },
            method: "POST",
            token,
          })
        )
      );

      results.forEach((result, index) => {
        const candidate = toStage[index];
        if (result.status === "fulfilled" && result.value) {
          const created = result.value;
          createdQuestionIds.push(created.question_id);
          updatesById.set(candidate.local_id, {
            error: "",
            selected: false,
            staged_question_id: created.question_id,
            status: "staged",
          });
          return;
        }
        const stageError =
          result.status === "rejected"
            ? result.reason?.message || String(result.reason || "Failed to stage candidate.")
            : "Failed to stage candidate.";
        updatesById.set(candidate.local_id, {
          error: stageError,
          status: "error",
        });
      });

      setExtractionCandidates((current) =>
        current.map((item) =>
          updatesById.has(item.local_id) ? { ...item, ...updatesById.get(item.local_id) } : item
        )
      );

      if (createdQuestionIds.length > 0) {
        const existingTargets = Array.isArray(extractionNote.targets) ? extractionNote.targets : [];
        const nextTargets = [...existingTargets];
        for (const questionId of createdQuestionIds) {
          if (
            nextTargets.some(
              (target) => target.entity_type === "question" && target.entity_id === questionId
            )
          ) {
            continue;
          }
          nextTargets.push({ entity_id: questionId, entity_type: "question" });
        }
        if (nextTargets.length !== existingTargets.length) {
          const updatedNote = await apiRequest(`/notes/${extractionNote.note_id}`, {
            body: { targets: nextTargets },
            method: "PATCH",
            token,
          });
          setExtractionNote(updatedNote);
        }
      }

      await refreshProjectData(selectedProjectId);
      setFlash(
        createdQuestionIds.length === 1
          ? "Staged 1 question from candidates."
          : `Staged ${createdQuestionIds.length} questions from candidates.`
      );
    } catch (err) {
      setFlash("", err.message || "Failed to stage candidates.");
    } finally {
      setBusy(false);
    }
  }

  async function handleUploadDatasetFiles(datasetId, files) {
    if (!canWrite) {
      return;
    }
    const selected = Array.isArray(files) ? files : [];
    if (selected.length === 0) {
      setFlash("", "Select at least one file to attach.");
      return;
    }

    let uploadedCount = 0;
    setBusy(true);
    setFlash("", "");
    try {
      for (const file of selected) {
        const formData = new FormData();
        formData.append("file", file);
        await apiRequest(`/datasets/${datasetId}/files`, {
          body: formData,
          method: "POST",
          token,
        });
        uploadedCount += 1;
      }
      setFlash(uploadedCount === 1 ? "Dataset file attached." : "Dataset files attached.");
    } catch (err) {
      setFlash("", err.message || "Failed to attach dataset file.");
    } finally {
      if (uploadedCount > 0) {
        await loadDatasetFiles(datasetId);
      }
      setBusy(false);
    }
  }

  async function handleDeleteDatasetFile(datasetId, fileId) {
    if (!canWrite) {
      return;
    }
    setBusy(true);
    setFlash("", "");
    try {
      await apiRequest(`/datasets/${datasetId}/files/${fileId}`, {
        method: "DELETE",
        token,
      });
      await loadDatasetFiles(datasetId);
      setFlash("Dataset file removed.");
    } catch (err) {
      setFlash("", err.message || "Failed to remove dataset file.");
    } finally {
      setBusy(false);
    }
  }

  async function handleCreateDataset(event) {
    event.preventDefault();
    if (!selectedProjectId || !canWrite) {
      return;
    }
    if (!datasetPrimaryQuestionId) {
      setFlash("", "Pick a primary question for the dataset.");
      return;
    }

    const secondaryQuestionIds = datasetSecondaryRaw
      .split(",")
      .map((item) => item.trim())
      .filter((item) => item && item !== datasetPrimaryQuestionId);

    setBusy(true);
    setFlash("", "");
    try {
      await apiRequest("/datasets", {
        body: {
          primary_question_id: datasetPrimaryQuestionId,
          project_id: selectedProjectId,
          secondary_question_ids: secondaryQuestionIds,
        },
        method: "POST",
        token,
      });
      setDatasetSecondaryRaw("");
      await refreshProjectData(selectedProjectId);
      setFlash("Dataset staged.");
    } catch (err) {
      setFlash("", err.message || "Failed to create dataset.");
    } finally {
      setBusy(false);
    }
  }

  async function handleCommitDataset(datasetId) {
    if (!canWrite) {
      return;
    }
    const fileState = datasetFilesById[datasetId];
    if (
      fileState &&
      fileState.loaded &&
      !fileState.loading &&
      !fileState.error &&
      fileState.items.length === 0
    ) {
      setFlash("", "Attach at least one file before committing a dataset.");
      return;
    }
    setBusy(true);
    setFlash("", "");
    try {
      const updated = await apiRequest(`/datasets/${datasetId}`, {
        body: { status: "committed" },
        method: "PATCH",
        token,
      });
      await refreshProjectData(selectedProjectId);
      if (updated && updated.status === "committed") {
        setFlash("Dataset committed.");
      } else {
        setFlash("Commit requested. Awaiting PI review.");
      }
    } catch (err) {
      setFlash("", err.message || "Failed to commit dataset.");
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

  async function handleCreateAnalysis(event) {
    event.preventDefault();
    if (!selectedProjectId || !canWrite) {
      return;
    }
    if (analysisDatasetIds.length === 0) {
      setFlash("", "Select at least one dataset for the analysis.");
      return;
    }
    if (!analysisCodeVersion.trim()) {
      setFlash("", "code_version is required.");
      return;
    }
    if (!analysisMethodHash.trim()) {
      setFlash("", "method_hash is required.");
      return;
    }

    setBusy(true);
    setFlash("", "");
    try {
      await apiRequest("/analyses", {
        body: {
          code_version: analysisCodeVersion.trim(),
          dataset_ids: analysisDatasetIds,
          environment_hash: analysisEnvironmentHash.trim() || null,
          method_hash: analysisMethodHash.trim(),
          project_id: selectedProjectId,
        },
        method: "POST",
        token,
      });
      setAnalysisDatasetIds([]);
      setAnalysisCodeVersion("");
      setAnalysisMethodHash("");
      setAnalysisEnvironmentHash("");
      await refreshProjectData(selectedProjectId);
      setFlash("Analysis staged.");
    } catch (err) {
      setFlash("", err.message || "Failed to create analysis.");
    } finally {
      setBusy(false);
    }
  }

  async function handleCommitAnalysis(analysisId) {
    if (!canWrite) {
      return;
    }
    setBusy(true);
    setFlash("", "");
    try {
      await apiRequest(`/analyses/${analysisId}/commit`, {
        body: {},
        method: "POST",
        token,
      });
      await refreshProjectData(selectedProjectId);
      setFlash("Analysis committed.");
    } catch (err) {
      setFlash("", err.message || "Failed to commit analysis.");
    } finally {
      setBusy(false);
    }
  }

  async function handleArchiveAnalysis(analysisId) {
    if (!canWrite) {
      return;
    }
    setBusy(true);
    setFlash("", "");
    try {
      await apiRequest(`/analyses/${analysisId}`, {
        body: { status: "archived" },
        method: "PATCH",
        token,
      });
      await refreshProjectData(selectedProjectId);
      setFlash("Analysis archived.");
    } catch (err) {
      setFlash("", err.message || "Failed to archive analysis.");
    } finally {
      setBusy(false);
    }
  }

  return {
    activeQuestions,
    analyses,
    analysisCodeVersion,
    analysisDatasetIds,
    analysisEnvironmentHash,
    analysisMethodHash,
    authBusy,
    authMode,
    authPassword,
    authUsername,
    busy,
    canWrite,
    datasetFilesById,
    datasetPrimaryQuestionId,
    datasetReviewsById,
    datasetSecondaryRaw,
    datasets,
    error,
    extractionCandidates,
    extractionNote,
    extractionNoteId,
    extractionNoteRaw,
    extractionNoteRawError,
    handleActivateQuestion,
    handleArchiveAnalysis,
    handleAuthSubmit,
    handleClearCandidateSelection,
    handleCloseSession,
    handleCommitAnalysis,
    handleCommitDataset,
    handleCreateAnalysis,
    handleCreateDataset,
    handleCreateProject,
    handleCreateQuestion,
    handleCreateSession,
    handleCreateTextNote,
    handleDeleteDatasetFile,
    handleExtractQuestionCandidates,
    handleExtractionNoteIdChange,
    handleLogout,
    handlePromoteSession,
    handleRejectExtractionCandidates,
    handleSelectAllPendingCandidates,
    handleStageExtractionCandidates,
    handleToggleExtractionCandidateSelected,
    handleUpdateExtractionCandidate,
    handleUploadDatasetFiles,
    handleUploadNote,
    loadDatasetFiles,
    message,
    navigate,
    noteText,
    notes,
    projectDescription,
    projectName,
    projects,
    questionHypothesis,
    questionText,
    questionType,
    questions,
    refreshActiveProject,
    route,
    selectedProject,
    selectedProjectId,
    sessionPrimaryQuestionId,
    sessionType,
    sessions,
    setAnalysisCodeVersion,
    setAnalysisDatasetIds,
    setAnalysisEnvironmentHash,
    setAnalysisMethodHash,
    setAuthMode,
    setAuthPassword,
    setAuthUsername,
    setDatasetPrimaryQuestionId,
    setDatasetSecondaryRaw,
    setFlash,
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
    token,
    uploadTargetQuestionId,
    uploadTranscript,
    user,
    visualizations,
  };
}

export { useLabTrackerAppState };
