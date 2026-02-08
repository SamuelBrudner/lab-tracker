import * as React from "react";
import * as ReactDOM from "react-dom/client";

const { useEffect, useMemo, useState } = React;

const TOKEN_STORAGE_KEY = "lab_tracker_access_token";
const QUESTION_TYPES = [
  "descriptive",
  "hypothesis_driven",
  "method_dev",
  "other",
];

function parseApiError(payload, fallbackMessage) {
  if (!payload || typeof payload !== "object") {
    return fallbackMessage;
  }
  if (payload.error && payload.error.message) {
    return payload.error.message;
  }
  return fallbackMessage;
}

async function apiRequest(path, options = {}) {
  const {
    method = "GET",
    token = "",
    body = null,
  } = options;
  const headers = {
    Accept: "application/json",
  };
  if (token) {
    headers.Authorization = `Bearer ${token}`;
  }
  if (body !== null) {
    headers["Content-Type"] = "application/json";
  }

  const response = await fetch(path, {
    method,
    headers,
    body: body === null ? undefined : JSON.stringify(body),
  });

  const isJson = (response.headers.get("content-type") || "").includes(
    "application/json"
  );
  const payload = isJson ? await response.json() : null;

  if (!response.ok) {
    throw new Error(parseApiError(payload, `Request failed with ${response.status}`));
  }

  if (!payload || !Object.prototype.hasOwnProperty.call(payload, "data")) {
    return null;
  }
  return payload.data;
}

function toBase64Content(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(new Error("Unable to read file."));
    reader.onload = () => {
      const value = String(reader.result || "");
      const marker = value.indexOf(",");
      if (marker < 0) {
        reject(new Error("Unable to parse uploaded file."));
        return;
      }
      resolve(value.slice(marker + 1));
    };
    reader.readAsDataURL(file);
  });
}

function formatDate(value) {
  if (!value) {
    return "";
  }
  try {
    return new Date(value).toLocaleString();
  } catch {
    return value;
  }
}

function roleClass(role) {
  return `pill role-${role || "viewer"}`;
}

function App() {
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

  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");

  const canWrite = user && (user.role === "admin" || user.role === "editor");

  const stagedQuestions = useMemo(
    () => questions.filter((item) => item.status === "staged"),
    [questions]
  );
  const activeQuestions = useMemo(
    () => questions.filter((item) => item.status === "active"),
    [questions]
  );

  function setFlash(nextMessage, nextError = "") {
    setMessage(nextMessage);
    setError(nextError);
  }

  async function bootstrapSession(nextToken) {
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
    }
  }

  async function refreshProjectData(projectId) {
    if (!projectId || !token) {
      return;
    }
    const encodedProjectId = encodeURIComponent(projectId);
    const [nextQuestions, nextDatasets, nextNotes] = await Promise.all([
      apiRequest(`/questions?project_id=${encodedProjectId}&limit=200`, { token }),
      apiRequest(`/datasets?project_id=${encodedProjectId}&limit=200`, { token }),
      apiRequest(`/notes?project_id=${encodedProjectId}&limit=200`, { token }),
    ]);
    setQuestions(nextQuestions);
    setDatasets(nextDatasets);
    setNotes(nextNotes);

    if (!datasetPrimaryQuestionId && nextQuestions.length > 0) {
      setDatasetPrimaryQuestionId(nextQuestions[0].question_id);
    }
  }

  async function refreshProjects() {
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
      return;
    }
    setSelectedProjectId((current) => {
      if (current && nextProjects.some((item) => item.project_id === current)) {
        return current;
      }
      return nextProjects[0].project_id;
    });
  }

  useEffect(() => {
    if (token) {
      localStorage.setItem(TOKEN_STORAGE_KEY, token);
    } else {
      localStorage.removeItem(TOKEN_STORAGE_KEY);
    }
  }, [token]);

  useEffect(() => {
    let canceled = false;

    if (!token) {
      setUser(null);
      setProjects([]);
      setSelectedProjectId("");
      setQuestions([]);
      setDatasets([]);
      setNotes([]);
      return () => {
        canceled = true;
      };
    }

    setBusy(true);
    setFlash("", "");
    bootstrapSession(token)
      .catch((err) => {
        if (canceled) {
          return;
        }
        setToken("");
        setFlash("", err.message || "Failed to restore session.");
      })
      .finally(() => {
        if (!canceled) {
          setBusy(false);
        }
      });

    return () => {
      canceled = true;
    };
  }, [token]);

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
        if (canceled) {
          return;
        }
        setFlash("", err.message || "Unable to load project data.");
      })
      .finally(() => {
        if (!canceled) {
          setBusy(false);
        }
      });

    return () => {
      canceled = true;
    };
  }, [selectedProjectId, token]);

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
      const body = {
        username: authUsername.trim(),
        password: authPassword,
      };
      const payload = await apiRequest(endpoint, {
        method: "POST",
        body,
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
        method: "POST",
        token,
        body: {
          name: projectName.trim(),
          description: projectDescription.trim() || null,
        },
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
        method: "POST",
        token,
        body: {
          project_id: selectedProjectId,
          text: questionText.trim(),
          question_type: questionType,
          hypothesis: questionHypothesis.trim() || null,
        },
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
        method: "PATCH",
        token,
        body: { status: "active" },
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
        method: "POST",
        token,
        body: {
          project_id: selectedProjectId,
          raw_content: noteText.trim(),
        },
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
        project_id: selectedProjectId,
        filename: uploadFile.name,
        content_type: uploadFile.type || "application/octet-stream",
        content_base64: encoded,
        transcribed_text: uploadTranscript.trim() || null,
      };
      if (uploadTargetQuestionId) {
        payload.targets = [
          {
            entity_type: "question",
            entity_id: uploadTargetQuestionId,
          },
        ];
      }

      await apiRequest("/notes/upload", {
        method: "POST",
        token,
        body: payload,
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
        method: "POST",
        token,
        body: {
          project_id: selectedProjectId,
          primary_question_id: datasetPrimaryQuestionId,
          secondary_question_ids: secondaryQuestionIds,
        },
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
    setBusy(true);
    setFlash("", "");
    try {
      await apiRequest(`/datasets/${datasetId}`, {
        method: "PATCH",
        token,
        body: { status: "committed" },
      });
      await refreshProjectData(selectedProjectId);
      setFlash("Dataset committed.");
    } catch (err) {
      setFlash("", err.message || "Failed to commit dataset.");
    } finally {
      setBusy(false);
    }
  }

  const selectedProject = projects.find((item) => item.project_id === selectedProjectId) || null;

  return (
    <div className="app-shell">
      <header className="hero">
        <div className="hero-row">
          <div>
            <h1>Lab Tracker Frontend MVP</h1>
            <p className="subtle">
              Project dashboard, staged question review, note capture, and dataset commit workflow.
            </p>
          </div>
          <div className="inline">
            {user ? <span className={roleClass(user.role)}>{user.role}</span> : null}
            {user ? <span className="pill">{user.username}</span> : null}
            {user ? (
              <button className="btn-secondary" onClick={handleLogout}>
                Sign out
              </button>
            ) : null}
          </div>
        </div>
      </header>

      {message ? <p className="flash ok">{message}</p> : null}
      {error ? <p className="flash error">{error}</p> : null}

      {!token ? (
        <section className="grid">
          <article className="card span-6">
            <h2>{authMode === "login" ? "Sign In" : "Create Viewer Account"}</h2>
            <p className="subtle">
              Viewer registration is public. Admin/editor accounts must be provisioned by an admin.
            </p>
            <form className="form" onSubmit={handleAuthSubmit}>
              <label>
                Username
                <input
                  value={authUsername}
                  onChange={(event) => setAuthUsername(event.target.value)}
                  autoComplete="username"
                />
              </label>
              <label>
                Password
                <input
                  type="password"
                  value={authPassword}
                  onChange={(event) => setAuthPassword(event.target.value)}
                  autoComplete={authMode === "login" ? "current-password" : "new-password"}
                />
              </label>
              <div className="inline">
                <button className="btn-primary" disabled={authBusy}>
                  {authBusy ? "Working..." : authMode === "login" ? "Sign in" : "Register"}
                </button>
                <button
                  type="button"
                  className="btn-secondary"
                  onClick={() => setAuthMode((current) => (current === "login" ? "register" : "login"))}
                >
                  {authMode === "login" ? "Need an account?" : "Have an account?"}
                </button>
              </div>
            </form>
          </article>
          <article className="card span-6">
            <h2>Workflow Coverage</h2>
            <div className="stack">
              <div className="item">1. Project dashboard and project creation</div>
              <div className="item">2. Question capture and staged-to-active commit</div>
              <div className="item">3. Text notes and photo uploads</div>
              <div className="item">4. Dataset staging and dataset commit review</div>
            </div>
          </article>
        </section>
      ) : (
        <section className="grid">
          <article className="card span-4">
            <h2>Dashboard</h2>
            <div className="inline">
              <div className="kpi">
                <span className="subtle">Projects</span>
                <strong>{projects.length}</strong>
              </div>
              <div className="kpi">
                <span className="subtle">Questions</span>
                <strong>{questions.length}</strong>
              </div>
              <div className="kpi">
                <span className="subtle">Datasets</span>
                <strong>{datasets.length}</strong>
              </div>
              <div className="kpi">
                <span className="subtle">Notes</span>
                <strong>{notes.length}</strong>
              </div>
            </div>

            <label>
              Active project
              <select
                value={selectedProjectId}
                onChange={(event) => setSelectedProjectId(event.target.value)}
              >
                <option value="">Select a project</option>
                {projects.map((project) => (
                  <option key={project.project_id} value={project.project_id}>
                    {project.name}
                  </option>
                ))}
              </select>
            </label>

            <form className="form" onSubmit={handleCreateProject}>
              <h3>New Project</h3>
              <label>
                Name
                <input
                  value={projectName}
                  onChange={(event) => setProjectName(event.target.value)}
                  disabled={!canWrite}
                />
              </label>
              <label>
                Description
                <textarea
                  value={projectDescription}
                  onChange={(event) => setProjectDescription(event.target.value)}
                  disabled={!canWrite}
                />
              </label>
              <button className="btn-primary" disabled={!canWrite || busy}>
                Create project
              </button>
            </form>

            {!canWrite ? (
              <p className="warn">
                Your role is read-only. Ask an admin to provision an editor or admin account for write
                workflows.
              </p>
            ) : null}
          </article>

          <article className="card span-8">
            <h2>Question Staging & Commit</h2>
            <p className="subtle">
              Capture questions into staging, then activate when ready for use in acquisition and dataset
              creation.
            </p>

            <form className="form" onSubmit={handleCreateQuestion}>
              <label>
                Question text
                <textarea
                  value={questionText}
                  onChange={(event) => setQuestionText(event.target.value)}
                  disabled={!canWrite || !selectedProjectId}
                />
              </label>
              <label>
                Question type
                <select
                  value={questionType}
                  onChange={(event) => setQuestionType(event.target.value)}
                  disabled={!canWrite || !selectedProjectId}
                >
                  {QUESTION_TYPES.map((typeValue) => (
                    <option value={typeValue} key={typeValue}>
                      {typeValue}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                Hypothesis (optional)
                <input
                  value={questionHypothesis}
                  onChange={(event) => setQuestionHypothesis(event.target.value)}
                  disabled={!canWrite || !selectedProjectId}
                />
              </label>
              <button className="btn-primary" disabled={!canWrite || !selectedProjectId || busy}>
                Stage question
              </button>
            </form>

            <h3>Staging Inbox</h3>
            {stagedQuestions.length === 0 ? (
              <p className="subtle">No staged questions for this project.</p>
            ) : (
              <div className="stack">
                {stagedQuestions.map((question) => (
                  <article key={question.question_id} className="item">
                    <div className="item-head">
                      <strong>{question.text}</strong>
                      <span className="pill">{question.question_type}</span>
                    </div>
                    <p className="mono">{question.question_id}</p>
                    <div className="inline">
                      <button
                        className="btn-primary"
                        disabled={!canWrite || busy}
                        onClick={() => handleActivateQuestion(question.question_id)}
                      >
                        Commit (activate)
                      </button>
                    </div>
                  </article>
                ))}
              </div>
            )}
          </article>

          <article className="card span-6">
            <h2>Note Capture</h2>
            <form className="form" onSubmit={handleCreateTextNote}>
              <h3>Quick text note</h3>
              <label>
                Raw note text
                <textarea
                  value={noteText}
                  onChange={(event) => setNoteText(event.target.value)}
                  disabled={!canWrite || !selectedProjectId}
                />
              </label>
              <button className="btn-secondary" disabled={!canWrite || !selectedProjectId || busy}>
                Save text note
              </button>
            </form>

            <form className="form" onSubmit={handleUploadNote}>
              <h3>Photo upload</h3>
              <label>
                Select image/file
                <input
                  type="file"
                  accept="image/*"
                  onChange={(event) => setUploadFile(event.target.files?.[0] || null)}
                  disabled={!canWrite || !selectedProjectId}
                />
              </label>
              <label>
                Link to active question (optional)
                <select
                  value={uploadTargetQuestionId}
                  onChange={(event) => setUploadTargetQuestionId(event.target.value)}
                  disabled={!canWrite || !selectedProjectId}
                >
                  <option value="">No question link</option>
                  {activeQuestions.map((question) => (
                    <option value={question.question_id} key={question.question_id}>
                      {question.text}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                Transcribed text (optional)
                <textarea
                  value={uploadTranscript}
                  onChange={(event) => setUploadTranscript(event.target.value)}
                  disabled={!canWrite || !selectedProjectId}
                />
              </label>
              <button className="btn-primary" disabled={!canWrite || !selectedProjectId || busy}>
                Upload photo note
              </button>
            </form>

            <h3>Recent Notes</h3>
            <div className="stack">
              {notes.slice(0, 5).map((note) => (
                <article className="item" key={note.note_id}>
                  <div className="item-head">
                    <span className="pill">{note.status}</span>
                    <span className="subtle">{formatDate(note.created_at)}</span>
                  </div>
                  <p>{note.transcribed_text || note.raw_content || "(binary upload)"}</p>
                  <p className="mono">{note.note_id}</p>
                </article>
              ))}
            </div>
          </article>

          <article className="card span-6">
            <h2>Dataset Review</h2>
            <p className="subtle">
              Stage datasets against active questions, then commit after review.
            </p>

            <form className="form" onSubmit={handleCreateDataset}>
              <label>
                Primary question
                <select
                  value={datasetPrimaryQuestionId}
                  onChange={(event) => setDatasetPrimaryQuestionId(event.target.value)}
                  disabled={!canWrite || !selectedProjectId || questions.length === 0}
                >
                  <option value="">Select question</option>
                  {questions.map((question) => (
                    <option value={question.question_id} key={question.question_id}>
                      {question.text}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                Secondary question IDs (comma-separated UUIDs)
                <input
                  value={datasetSecondaryRaw}
                  onChange={(event) => setDatasetSecondaryRaw(event.target.value)}
                  disabled={!canWrite || !selectedProjectId}
                />
              </label>
              <button className="btn-secondary" disabled={!canWrite || !selectedProjectId || busy}>
                Stage dataset
              </button>
            </form>

            <div className="stack">
              {datasets.map((dataset) => (
                <article className="item" key={dataset.dataset_id}>
                  <div className="item-head">
                    <strong>{dataset.status}</strong>
                    <span className="subtle">{formatDate(dataset.created_at)}</span>
                  </div>
                  <p className="mono">{dataset.dataset_id}</p>
                  <p className="mono">commit hash: {dataset.commit_hash}</p>
                  <p>
                    Links: {dataset.question_links.map((link) => `${link.role}:${link.question_id}`).join(" | ")}
                  </p>
                  {dataset.status !== "committed" ? (
                    <button
                      className="btn-primary"
                      disabled={!canWrite || busy}
                      onClick={() => handleCommitDataset(dataset.dataset_id)}
                    >
                      Commit dataset
                    </button>
                  ) : null}
                </article>
              ))}
            </div>
          </article>

          <article className="card span-12">
            <h2>Project Context</h2>
            {selectedProject ? (
              <div>
                <strong>{selectedProject.name}</strong>
                <p>{selectedProject.description || "No project description."}</p>
                <p className="mono">{selectedProject.project_id}</p>
              </div>
            ) : (
              <p className="subtle">Create or select a project to start the workflow.</p>
            )}
          </article>
        </section>
      )}

      {busy ? <p className="subtle">Syncing...</p> : null}
    </div>
  );
}

const root = ReactDOM.createRoot(document.getElementById("app-root"));
root.render(<App />);
