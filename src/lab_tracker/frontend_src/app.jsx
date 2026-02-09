import * as React from "react";
import * as ReactDOM from "react-dom/client";

const { useCallback, useEffect, useMemo, useState } = React;

const TOKEN_STORAGE_KEY = "lab_tracker_access_token";
const QUESTION_TYPES = [
  "descriptive",
  "hypothesis_driven",
  "method_dev",
  "other",
];

class ErrorBoundary extends React.Component {
  constructor(props) {
    super(props);
    this.state = {
      hasError: false,
      errorMessage: "",
      componentStack: "",
      resetKey: 0,
    };
    this.handleRetry = this.handleRetry.bind(this);
    this.handleReload = this.handleReload.bind(this);
  }

  static getDerivedStateFromError(error) {
    let errorMessage = "Unknown error.";
    if (error instanceof Error && error.message) {
      errorMessage = error.message;
    } else if (typeof error === "string") {
      errorMessage = error;
    } else if (error && typeof error === "object" && "message" in error) {
      errorMessage = String(error.message || errorMessage);
    } else if (error) {
      errorMessage = String(error);
    }

    return { hasError: true, errorMessage };
  }

  componentDidCatch(error, errorInfo) {
    // eslint-disable-next-line no-console
    console.error("React rendering error:", error, errorInfo);
    const componentStack =
      errorInfo && typeof errorInfo.componentStack === "string" ? errorInfo.componentStack : "";
    if (componentStack) {
      this.setState({ componentStack });
    }
  }

  handleRetry() {
    this.setState((current) => ({
      hasError: false,
      errorMessage: "",
      componentStack: "",
      resetKey: current.resetKey + 1,
    }));
  }

  handleReload() {
    window.location.reload();
  }

  render() {
    if (this.state.hasError) {
      return (
        <div className="app-shell">
          <header className="hero">
            <div className="hero-row">
              <div>
                <h1>Lab Tracker Frontend MVP</h1>
                <p className="subtle">The app hit an unexpected error.</p>
              </div>
            </div>
          </header>

          <section className="grid">
            <article className="card span-12">
              <h2>Something went wrong</h2>
              <p className="subtle">
                Click &quot;Try again&quot; to re-render the app. If the problem persists, reload the
                page.
              </p>

              <div className="inline">
                <button type="button" className="btn-primary" onClick={this.handleRetry}>
                  Try again
                </button>
                <button type="button" className="btn-secondary" onClick={this.handleReload}>
                  Reload page
                </button>
              </div>

              {this.state.errorMessage ? (
                <p className="flash error">Error: {this.state.errorMessage}</p>
              ) : null}

              {process.env.NODE_ENV !== "production" && this.state.componentStack ? (
                <details className="subtle">
                  <summary>Details</summary>
                  <pre className="mono">{this.state.componentStack}</pre>
                </details>
              ) : null}
            </article>
          </section>
        </div>
      );
    }

    return <React.Fragment key={this.state.resetKey}>{this.props.children}</React.Fragment>;
  }
}

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
  const isFormData = typeof FormData !== "undefined" && body instanceof FormData;
  const headers = {
    Accept: "application/json",
  };
  if (token) {
    headers.Authorization = `Bearer ${token}`;
  }
  if (body !== null && !isFormData) {
    headers["Content-Type"] = "application/json";
  }

  const response = await fetch(path, {
    method,
    headers,
    body: body === null ? undefined : isFormData ? body : JSON.stringify(body),
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

function formatBytes(value) {
  if (value === null || value === undefined) {
    return "";
  }
  const size = Number(value);
  if (!Number.isFinite(size) || size < 0) {
    return String(value);
  }
  if (size < 1024) {
    return `${size} B`;
  }
  const units = ["KB", "MB", "GB", "TB"];
  let next = size;
  let unitIndex = -1;
  while (next >= 1024 && unitIndex < units.length - 1) {
    next /= 1024;
    unitIndex += 1;
  }
  const rounded = next >= 10 ? Math.round(next) : Math.round(next * 10) / 10;
  return `${rounded} ${units[unitIndex]}`;
}

function roleClass(role) {
  return `pill role-${role || "viewer"}`;
}

const UUID_RE =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

function parseAppRoute(pathname) {
  const parts = String(pathname || "")
    .split("/")
    .filter(Boolean);
  if (parts.length === 0) {
    return { kind: "home" };
  }
  if (parts[0] !== "app") {
    return { kind: "home" };
  }
  if (parts.length === 1) {
    return { kind: "home" };
  }
  if (parts.length >= 3 && parts[1] === "questions" && UUID_RE.test(parts[2] || "")) {
    return { kind: "question", questionId: parts[2] };
  }
  if (parts.length >= 3 && parts[1] === "notes" && UUID_RE.test(parts[2] || "")) {
    return { kind: "note", noteId: parts[2] };
  }
  return { kind: "unknown", pathname: `/${parts.join("/")}` };
}

function AppLink({ to, navigate, className = "", children }) {
  return (
    <a
      href={to}
      className={className}
      onClick={(event) => {
        if (
          event.defaultPrevented ||
          event.button !== 0 ||
          event.metaKey ||
          event.altKey ||
          event.ctrlKey ||
          event.shiftKey
        ) {
          return;
        }
        event.preventDefault();
        navigate(to);
      }}
    >
      {children}
    </a>
  );
}

function AppHeader({ user, onLogout }) {
  return (
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
            <button className="btn-secondary" onClick={onLogout}>
              Sign out
            </button>
          ) : null}
        </div>
      </div>
    </header>
  );
}

function FlashMessages({ message, error }) {
  if (!message && !error) {
    return null;
  }

  return (
    <>
      {message ? <p className="flash ok">{message}</p> : null}
      {error ? <p className="flash error">{error}</p> : null}
    </>
  );
}

function AuthForm({
  authMode,
  authUsername,
  authPassword,
  authBusy,
  onSubmit,
  onUsernameChange,
  onPasswordChange,
  onToggleMode,
}) {
  return (
    <article className="card span-6">
      <h2>{authMode === "login" ? "Sign In" : "Create Viewer Account"}</h2>
      <p className="subtle">
        Viewer registration is public. Admin/editor accounts must be provisioned by an admin.
      </p>
      <form className="form" onSubmit={onSubmit}>
        <label>
          Username
          <input value={authUsername} onChange={onUsernameChange} autoComplete="username" />
        </label>
        <label>
          Password
          <input
            type="password"
            value={authPassword}
            onChange={onPasswordChange}
            autoComplete={authMode === "login" ? "current-password" : "new-password"}
          />
        </label>
        <div className="inline">
          <button className="btn-primary" disabled={authBusy}>
            {authBusy ? "Working..." : authMode === "login" ? "Sign in" : "Register"}
          </button>
          <button type="button" className="btn-secondary" onClick={onToggleMode}>
            {authMode === "login" ? "Need an account?" : "Have an account?"}
          </button>
        </div>
      </form>
    </article>
  );
}

function WorkflowCoverageCard() {
  return (
    <article className="card span-6">
      <h2>Workflow Coverage</h2>
      <div className="stack">
        <div className="item">1. Project dashboard and project creation</div>
        <div className="item">2. Question capture and staged-to-active commit</div>
        <div className="item">3. Text notes and photo uploads</div>
        <div className="item">4. Dataset staging and dataset commit review</div>
      </div>
    </article>
  );
}

function Dashboard({
  projects,
  questions,
  datasets,
  notes,
  selectedProjectId,
  onSelectedProjectChange,
  canWrite,
  busy,
  projectName,
  projectDescription,
  onProjectNameChange,
  onProjectDescriptionChange,
  onCreateProject,
}) {
  return (
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
        <select value={selectedProjectId} onChange={onSelectedProjectChange}>
          <option value="">Select a project</option>
          {projects.map((project) => (
            <option key={project.project_id} value={project.project_id}>
              {project.name}
            </option>
          ))}
        </select>
      </label>

      <form className="form" onSubmit={onCreateProject}>
        <h3>New Project</h3>
        <label>
          Name
          <input value={projectName} onChange={onProjectNameChange} disabled={!canWrite} />
        </label>
        <label>
          Description
          <textarea
            value={projectDescription}
            onChange={onProjectDescriptionChange}
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
  );
}

function QuestionPanel({
  canWrite,
  busy,
  selectedProjectId,
  questionText,
  questionType,
  questionHypothesis,
  onQuestionTextChange,
  onQuestionTypeChange,
  onQuestionHypothesisChange,
  onCreateQuestion,
  stagedQuestions,
  onActivateQuestion,
}) {
  return (
    <article className="card span-8">
      <h2>Question Staging & Commit</h2>
      <p className="subtle">
        Capture questions into staging, then activate when ready for use in acquisition and dataset
        creation.
      </p>

      <form className="form" onSubmit={onCreateQuestion}>
        <label>
          Question text
          <textarea
            value={questionText}
            onChange={onQuestionTextChange}
            disabled={!canWrite || !selectedProjectId}
          />
        </label>
        <label>
          Question type
          <select
            value={questionType}
            onChange={onQuestionTypeChange}
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
            onChange={onQuestionHypothesisChange}
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
                  onClick={() => onActivateQuestion(question.question_id)}
                >
                  Commit (activate)
                </button>
              </div>
            </article>
          ))}
        </div>
      )}
    </article>
  );
}

function NotePanel({
  canWrite,
  busy,
  selectedProjectId,
  noteText,
  onNoteTextChange,
  onCreateTextNote,
  onUploadNote,
  onUploadFileChange,
  uploadTargetQuestionId,
  onUploadTargetQuestionIdChange,
  uploadTranscript,
  onUploadTranscriptChange,
  activeQuestions,
  notes,
}) {
  return (
    <article className="card span-6">
      <h2>Note Capture</h2>
      <form className="form" onSubmit={onCreateTextNote}>
        <h3>Quick text note</h3>
        <label>
          Raw note text
          <textarea
            value={noteText}
            onChange={onNoteTextChange}
            disabled={!canWrite || !selectedProjectId}
          />
        </label>
        <button className="btn-secondary" disabled={!canWrite || !selectedProjectId || busy}>
          Save text note
        </button>
      </form>

      <form className="form" onSubmit={onUploadNote}>
        <h3>Photo upload</h3>
        <label>
          Select image/file
          <input
            type="file"
            accept="image/*"
            onChange={onUploadFileChange}
            disabled={!canWrite || !selectedProjectId}
          />
        </label>
        <label>
          Link to active question (optional)
          <select
            value={uploadTargetQuestionId}
            onChange={onUploadTargetQuestionIdChange}
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
            onChange={onUploadTranscriptChange}
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
  );
}

function DatasetPanel({
  canWrite,
  busy,
  selectedProjectId,
  datasetPrimaryQuestionId,
  onDatasetPrimaryQuestionIdChange,
  datasetSecondaryRaw,
  onDatasetSecondaryRawChange,
  onCreateDataset,
  questions,
  datasets,
  onCommitDataset,
  datasetFilesById,
  onLoadDatasetFiles,
  onUploadDatasetFiles,
  onDeleteDatasetFile,
}) {
  useEffect(() => {
    if (!selectedProjectId) {
      return;
    }

    datasets
      .filter((dataset) => dataset.status !== "committed")
      .forEach((dataset) => {
        const current = datasetFilesById[dataset.dataset_id];
        if (current) {
          return;
        }
        onLoadDatasetFiles(dataset.dataset_id);
      });
  }, [datasets, datasetFilesById, onLoadDatasetFiles, selectedProjectId]);

  return (
    <article className="card span-6">
      <h2>Dataset Review</h2>
      <p className="subtle">Stage datasets against active questions, then commit after review.</p>

      <form className="form" onSubmit={onCreateDataset}>
        <label>
          Primary question
          <select
            value={datasetPrimaryQuestionId}
            onChange={onDatasetPrimaryQuestionIdChange}
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
            onChange={onDatasetSecondaryRawChange}
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
              Links:{" "}
              {dataset.question_links
                .map((link) => `${link.role}:${link.question_id}`)
                .join(" | ")}
            </p>

            <div className="stack">
              <div className="item">
                <div className="item-head">
                  <strong>Files</strong>
                  <span className="subtle">
                    {dataset.status === "committed"
                      ? `${(dataset.commit_manifest?.files || []).length} committed`
                      : (() => {
                          const state = datasetFilesById[dataset.dataset_id];
                          if (!state || state.loading) {
                            return "loading...";
                          }
                          if (state.error) {
                            return "unavailable";
                          }
                          return `${state.items.length} attached`;
                        })()}
                  </span>
                </div>

                {dataset.status === "staged" ? (
                  <>
                    <label>
                      Attach file(s)
                      <input
                        type="file"
                        multiple
                        disabled={!canWrite || busy}
                        onChange={(event) => {
                          const files = Array.from(event.target.files || []);
                          event.target.value = "";
                          onUploadDatasetFiles(dataset.dataset_id, files);
                        }}
                      />
                    </label>
                  </>
                ) : null}

                {dataset.status === "committed" ? null : (() => {
                  const state = datasetFilesById[dataset.dataset_id];
                  if (!state) {
                    return null;
                  }
                  if (state.loading) {
                    return <p className="subtle">Loading attached files...</p>;
                  }
                  if (state.error) {
                    return (
                      <div className="stack">
                        <p className="subtle">Unable to load attached files: {state.error}</p>
                        <div className="inline">
                          <button
                            type="button"
                            className="btn-secondary"
                            disabled={busy}
                            onClick={() => onLoadDatasetFiles(dataset.dataset_id)}
                          >
                            Retry
                          </button>
                        </div>
                      </div>
                    );
                  }
                  if (state.items.length === 0) {
                    return (
                      <p className="warn">
                        Attach at least one file before committing this dataset.
                      </p>
                    );
                  }
                  return null;
                })()}

                <div className="stack">
                  {(dataset.status === "committed"
                    ? dataset.commit_manifest?.files || []
                    : datasetFilesById[dataset.dataset_id]?.items || []
                  ).map((file) => (
                    <div className="item" key={file.file_id || file.path}>
                      <div className="item-head">
                        <span className="mono">{file.path}</span>
                        <span className="subtle">{formatBytes(file.size_bytes)}</span>
                      </div>
                      <p className="mono">sha256: {file.checksum}</p>
                      {dataset.status === "staged" && file.file_id ? (
                        <button
                          type="button"
                          className="btn-danger"
                          disabled={!canWrite || busy}
                          onClick={() => onDeleteDatasetFile(dataset.dataset_id, file.file_id)}
                        >
                          Remove file
                        </button>
                      ) : null}
                    </div>
                  ))}
                </div>
              </div>
            </div>

            {dataset.status !== "committed" ? (
              <button
                type="button"
                className="btn-primary"
                disabled={(() => {
                  if (!canWrite || busy) {
                    return true;
                  }
                  const state = datasetFilesById[dataset.dataset_id];
                  if (!state || state.loading) {
                    return true;
                  }
                  if (state.error) {
                    return false;
                  }
                  return state.items.length === 0;
                })()}
                onClick={() => onCommitDataset(dataset.dataset_id)}
              >
                Commit dataset
              </button>
            ) : null}
          </article>
        ))}
      </div>
    </article>
  );
}

function ProjectContextCard({ selectedProject }) {
  return (
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
  );
}

function SearchPanel({ token, projects, selectedProjectId, navigate }) {
  const [query, setQuery] = useState("");
  const [projectFilter, setProjectFilter] = useState("__active__");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [results, setResults] = useState({ questions: [], notes: [] });
  const [lastRanQuery, setLastRanQuery] = useState("");

  function relevanceLevel(rank) {
    if (rank <= 0) {
      return { label: "Top", className: "pill relevance relevance-top" };
    }
    if (rank <= 2) {
      return { label: "High", className: "pill relevance relevance-high" };
    }
    if (rank <= 6) {
      return { label: "Med", className: "pill relevance relevance-med" };
    }
    return { label: "Low", className: "pill relevance relevance-low" };
  }

  function resolveProjectId() {
    if (projectFilter === "__active__") {
      return selectedProjectId || "";
    }
    return projectFilter;
  }

  async function runSearch(event) {
    event.preventDefault();
    if (!token) {
      return;
    }
    const trimmed = query.trim();
    if (!trimmed) {
      setError("");
      setResults({ questions: [], notes: [] });
      setLastRanQuery("");
      return;
    }
    const resolvedProjectId = resolveProjectId();
    if (projectFilter === "__active__" && !resolvedProjectId) {
      setError("Select an active project, or switch the search filter to All projects.");
      setResults({ questions: [], notes: [] });
      setLastRanQuery(trimmed);
      return;
    }

    setBusy(true);
    setError("");
    try {
      const params = new URLSearchParams();
      params.set("q", trimmed);
      params.set("include", "questions,notes");
      params.set("limit", "20");
      if (resolvedProjectId) {
        params.set("project_id", resolvedProjectId);
      }
      const payload = await apiRequest(`/search?${params.toString()}`, { token });
      setResults(payload || { questions: [], notes: [] });
      setLastRanQuery(trimmed);
    } catch (err) {
      setError(err.message || "Search failed.");
    } finally {
      setBusy(false);
    }
  }

  const projectOptions = useMemo(() => {
    const options = [
      { value: "__active__", label: "Active project" },
      { value: "", label: "All projects" },
      ...projects.map((project) => ({
        value: project.project_id,
        label: project.name,
      })),
    ];
    const seen = new Set();
    return options.filter((item) => {
      if (seen.has(item.value)) {
        return false;
      }
      seen.add(item.value);
      return true;
    });
  }, [projects]);

  const questionHits = results.questions || [];
  const noteHits = results.notes || [];
  const hasResults = questionHits.length > 0 || noteHits.length > 0;
  const showEmptyState = lastRanQuery && !busy && !error && !hasResults;

  return (
    <article className="card span-12">
      <div className="item-head">
        <h2>Search</h2>
        {busy ? <span className="pill">Searching...</span> : null}
      </div>
      <p className="subtle">
        Queries the semantic search endpoint across questions and notes. Results are ordered by
        backend relevance.
      </p>

      <form className="form" onSubmit={runSearch}>
        <div className="search-row">
          <label className="search-input">
            Query
            <input
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="e.g. photometry artifact, opto stimulation, trial alignment"
              disabled={!token}
            />
          </label>

          <label className="search-filter">
            Project filter
            <select
              value={projectFilter}
              onChange={(event) => setProjectFilter(event.target.value)}
              disabled={!token}
            >
              {projectOptions.map((option) => (
                <option key={option.value || "__all__"} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          </label>
        </div>

        <div className="inline">
          <button className="btn-primary" disabled={!token || busy || !query.trim()}>
            Search
          </button>
          <button
            type="button"
            className="btn-secondary"
            disabled={!token || busy || (!query && !hasResults && !error)}
            onClick={() => {
              setQuery("");
              setError("");
              setResults({ questions: [], notes: [] });
              setLastRanQuery("");
            }}
          >
            Clear
          </button>
        </div>
      </form>

      {error ? <p className="flash error">{error}</p> : null}

      {showEmptyState ? (
        <p className="subtle">No hits for &quot;{lastRanQuery}&quot;.</p>
      ) : null}

      {hasResults ? (
        <div className="grid search-results">
          <section className="card span-6">
            <div className="item-head">
              <h3>Questions</h3>
              <span className="pill">{questionHits.length}</span>
            </div>
            {questionHits.length === 0 ? (
              <p className="subtle">No matching questions.</p>
            ) : (
              <div className="stack">
                {questionHits.map((question, index) => {
                  const relevance = relevanceLevel(index);
                  return (
                    <article className="item" key={question.question_id}>
                      <div className="item-head">
                        <AppLink
                          to={`/app/questions/${question.question_id}`}
                          navigate={navigate}
                          className="link"
                        >
                          <strong>{question.text}</strong>
                        </AppLink>
                        <span className={relevance.className} title={`${relevance.label} relevance`}>
                          #{index + 1}
                        </span>
                      </div>
                      <div className="inline">
                        <span className="pill">{question.status}</span>
                        <span className="pill">{question.question_type}</span>
                      </div>
                      {question.hypothesis ? (
                        <p className="subtle">Hypothesis: {question.hypothesis}</p>
                      ) : null}
                      <p className="mono">{question.question_id}</p>
                    </article>
                  );
                })}
              </div>
            )}
          </section>

          <section className="card span-6">
            <div className="item-head">
              <h3>Notes</h3>
              <span className="pill">{noteHits.length}</span>
            </div>
            {noteHits.length === 0 ? (
              <p className="subtle">No matching notes.</p>
            ) : (
              <div className="stack">
                {noteHits.map((note, index) => {
                  const relevance = relevanceLevel(index);
                  const preview = note.transcribed_text || note.raw_content || "(binary upload)";
                  return (
                    <article className="item" key={note.note_id}>
                      <div className="item-head">
                        <AppLink
                          to={`/app/notes/${note.note_id}`}
                          navigate={navigate}
                          className="link"
                        >
                          <strong>{preview.slice(0, 110)}</strong>
                        </AppLink>
                        <span className={relevance.className} title={`${relevance.label} relevance`}>
                          #{index + 1}
                        </span>
                      </div>
                      <div className="inline">
                        <span className="pill">{note.status}</span>
                        <span className="subtle">{formatDate(note.created_at)}</span>
                      </div>
                      <p className="mono">{note.note_id}</p>
                    </article>
                  );
                })}
              </div>
            )}
          </section>
        </div>
      ) : null}
    </article>
  );
}

function UnknownRouteCard({ pathname, navigate }) {
  return (
    <article className="card span-8">
      <h2>Unknown View</h2>
      <p className="subtle">No route matches: {pathname}</p>
      <button type="button" className="btn-secondary" onClick={() => navigate("/app")}>
        Back to dashboard
      </button>
    </article>
  );
}

function QuestionDetailCard({ token, questionId, projects, navigate, onSetActiveProject }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [question, setQuestion] = useState(null);

  const project = useMemo(() => {
    if (!question) {
      return null;
    }
    return projects.find((item) => item.project_id === question.project_id) || null;
  }, [projects, question]);

  useEffect(() => {
    let canceled = false;
    if (!token || !questionId) {
      setQuestion(null);
      setError("");
      return () => {
        canceled = true;
      };
    }
    setBusy(true);
    setError("");
    apiRequest(`/questions/${questionId}`, { token })
      .then((payload) => {
        if (!canceled) {
          setQuestion(payload);
        }
      })
      .catch((err) => {
        if (!canceled) {
          setError(err.message || "Failed to load question.");
          setQuestion(null);
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
  }, [token, questionId]);

  return (
    <article className="card span-8">
      <div className="item-head">
        <h2>Question Detail</h2>
        {busy ? <span className="pill">Loading...</span> : null}
      </div>
      {error ? <p className="flash error">{error}</p> : null}
      {question ? (
        <div className="stack">
          <div className="inline">
            <span className="pill">{question.status}</span>
            <span className="pill">{question.question_type}</span>
            {project ? <span className="pill">{project.name}</span> : null}
          </div>
          <p>{question.text}</p>
          {question.hypothesis ? <p className="subtle">Hypothesis: {question.hypothesis}</p> : null}
          <div className="stack">
            <div className="subtle">Question ID</div>
            <div className="mono">{question.question_id}</div>
            <div className="subtle">Project ID</div>
            <div className="mono">{question.project_id}</div>
            <div className="subtle">Created</div>
            <div className="mono">{formatDate(question.created_at)}</div>
            <div className="subtle">Updated</div>
            <div className="mono">{formatDate(question.updated_at)}</div>
          </div>
        </div>
      ) : null}

      <div className="inline detail-actions">
        <button type="button" className="btn-secondary" onClick={() => navigate("/app")}>
          Back
        </button>
        {question ? (
          <button
            type="button"
            className="btn-primary"
            onClick={() => {
              onSetActiveProject(question.project_id);
              navigate("/app");
            }}
          >
            Set active project
          </button>
        ) : null}
      </div>
    </article>
  );
}

function NoteDetailCard({ token, noteId, projects, navigate, onSetActiveProject }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [note, setNote] = useState(null);

  const project = useMemo(() => {
    if (!note) {
      return null;
    }
    return projects.find((item) => item.project_id === note.project_id) || null;
  }, [projects, note]);

  useEffect(() => {
    let canceled = false;
    if (!token || !noteId) {
      setNote(null);
      setError("");
      return () => {
        canceled = true;
      };
    }
    setBusy(true);
    setError("");
    apiRequest(`/notes/${noteId}`, { token })
      .then((payload) => {
        if (!canceled) {
          setNote(payload);
        }
      })
      .catch((err) => {
        if (!canceled) {
          setError(err.message || "Failed to load note.");
          setNote(null);
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
  }, [token, noteId]);

  return (
    <article className="card span-8">
      <div className="item-head">
        <h2>Note Detail</h2>
        {busy ? <span className="pill">Loading...</span> : null}
      </div>
      {error ? <p className="flash error">{error}</p> : null}
      {note ? (
        <div className="stack">
          <div className="inline">
            <span className="pill">{note.status}</span>
            {project ? <span className="pill">{project.name}</span> : null}
          </div>
          <div className="stack">
            <div>
              <div className="subtle">Transcribed text</div>
              <p>{note.transcribed_text || <span className="subtle">(none)</span>}</p>
            </div>
            <div>
              <div className="subtle">Raw content</div>
              <p>{note.raw_content || <span className="subtle">(binary upload)</span>}</p>
            </div>
          </div>
          <div className="stack">
            <div className="subtle">Note ID</div>
            <div className="mono">{note.note_id}</div>
            <div className="subtle">Project ID</div>
            <div className="mono">{note.project_id}</div>
            <div className="subtle">Created</div>
            <div className="mono">{formatDate(note.created_at)}</div>
            <div className="subtle">Updated</div>
            <div className="mono">{formatDate(note.updated_at)}</div>
          </div>
        </div>
      ) : null}

      <div className="inline detail-actions">
        <button type="button" className="btn-secondary" onClick={() => navigate("/app")}>
          Back
        </button>
        {note ? (
          <button
            type="button"
            className="btn-primary"
            onClick={() => {
              onSetActiveProject(note.project_id);
              navigate("/app");
            }}
          >
            Set active project
          </button>
        ) : null}
      </div>
    </article>
  );
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
  const [datasetFilesById, setDatasetFilesById] = useState({});

  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");

  const [route, setRoute] = useState(() => parseAppRoute(window.location.pathname));

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
    setDatasetFilesById({});
  }, [selectedProjectId, token]);

  useEffect(() => {
    function handlePopState() {
      setRoute(parseAppRoute(window.location.pathname));
    }
    window.addEventListener("popstate", handlePopState);
    return () => window.removeEventListener("popstate", handlePopState);
  }, []);

  function navigate(to) {
    const resolved = String(to || "/app");
    if (resolved === window.location.pathname) {
      return;
    }
    window.history.pushState({}, "", resolved);
    setRoute(parseAppRoute(resolved));
  }

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
    window.history.replaceState({}, "", "/app");
    setRoute(parseAppRoute("/app"));
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

  const loadDatasetFiles = useCallback(
    async (datasetId) => {
      if (!token) {
        return null;
      }

      setDatasetFilesById((current) => ({
        ...current,
        [datasetId]: {
          items: current[datasetId]?.items || [],
          loaded: current[datasetId]?.loaded || false,
          loading: true,
          error: "",
        },
      }));

      try {
        const items = await apiRequest(`/datasets/${datasetId}/files?limit=200`, { token });
        const normalized = Array.isArray(items) ? items : [];
        setDatasetFilesById((current) => ({
          ...current,
          [datasetId]: {
            items: normalized,
            loaded: true,
            loading: false,
            error: "",
          },
        }));
        return normalized;
      } catch (err) {
        setDatasetFilesById((current) => ({
          ...current,
          [datasetId]: {
            items: current[datasetId]?.items || [],
            loaded: true,
            loading: false,
            error: err.message || "Failed to load dataset files.",
          },
        }));
        return null;
      }
    },
    [token]
  );

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
          method: "POST",
          token,
          body: formData,
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
      <AppHeader user={user} onLogout={handleLogout} />

      <FlashMessages message={message} error={error} />

      {!token ? (
        <section className="grid">
          <AuthForm
            authMode={authMode}
            authUsername={authUsername}
            authPassword={authPassword}
            authBusy={authBusy}
            onSubmit={handleAuthSubmit}
            onUsernameChange={(event) => setAuthUsername(event.target.value)}
            onPasswordChange={(event) => setAuthPassword(event.target.value)}
            onToggleMode={() =>
              setAuthMode((current) => (current === "login" ? "register" : "login"))
            }
          />
          <WorkflowCoverageCard />
        </section>
      ) : (
        <section className="grid">
          <SearchPanel
            token={token}
            projects={projects}
            selectedProjectId={selectedProjectId}
            navigate={navigate}
          />

          <Dashboard
            projects={projects}
            questions={questions}
            datasets={datasets}
            notes={notes}
            selectedProjectId={selectedProjectId}
            onSelectedProjectChange={(event) => setSelectedProjectId(event.target.value)}
            canWrite={canWrite}
            busy={busy}
            projectName={projectName}
            projectDescription={projectDescription}
            onProjectNameChange={(event) => setProjectName(event.target.value)}
            onProjectDescriptionChange={(event) => setProjectDescription(event.target.value)}
            onCreateProject={handleCreateProject}
          />

          {route.kind === "home" ? (
            <QuestionPanel
              canWrite={canWrite}
              busy={busy}
              selectedProjectId={selectedProjectId}
              questionText={questionText}
              questionType={questionType}
              questionHypothesis={questionHypothesis}
              onQuestionTextChange={(event) => setQuestionText(event.target.value)}
              onQuestionTypeChange={(event) => setQuestionType(event.target.value)}
              onQuestionHypothesisChange={(event) => setQuestionHypothesis(event.target.value)}
              onCreateQuestion={handleCreateQuestion}
              stagedQuestions={stagedQuestions}
              onActivateQuestion={handleActivateQuestion}
            />
          ) : null}

          {route.kind === "question" ? (
            <QuestionDetailCard
              token={token}
              questionId={route.questionId}
              projects={projects}
              navigate={navigate}
              onSetActiveProject={(projectId) => setSelectedProjectId(projectId)}
            />
          ) : null}

          {route.kind === "note" ? (
            <NoteDetailCard
              token={token}
              noteId={route.noteId}
              projects={projects}
              navigate={navigate}
              onSetActiveProject={(projectId) => setSelectedProjectId(projectId)}
            />
          ) : null}

          {route.kind === "unknown" ? (
            <UnknownRouteCard pathname={route.pathname} navigate={navigate} />
          ) : null}

          {route.kind === "home" ? (
            <NotePanel
              canWrite={canWrite}
              busy={busy}
              selectedProjectId={selectedProjectId}
              noteText={noteText}
              onNoteTextChange={(event) => setNoteText(event.target.value)}
              onCreateTextNote={handleCreateTextNote}
              onUploadNote={handleUploadNote}
              onUploadFileChange={(event) => setUploadFile(event.target.files?.[0] || null)}
              uploadTargetQuestionId={uploadTargetQuestionId}
              onUploadTargetQuestionIdChange={(event) =>
                setUploadTargetQuestionId(event.target.value)
              }
              uploadTranscript={uploadTranscript}
              onUploadTranscriptChange={(event) => setUploadTranscript(event.target.value)}
              activeQuestions={activeQuestions}
              notes={notes}
            />
          ) : null}

	          {route.kind === "home" ? (
	            <DatasetPanel
	              canWrite={canWrite}
	              busy={busy}
	              selectedProjectId={selectedProjectId}
	              datasetPrimaryQuestionId={datasetPrimaryQuestionId}
	              onDatasetPrimaryQuestionIdChange={(event) =>
	                setDatasetPrimaryQuestionId(event.target.value)
	              }
	              datasetSecondaryRaw={datasetSecondaryRaw}
	              onDatasetSecondaryRawChange={(event) => setDatasetSecondaryRaw(event.target.value)}
	              onCreateDataset={handleCreateDataset}
	              questions={questions}
	              datasets={datasets}
	              onCommitDataset={handleCommitDataset}
	              datasetFilesById={datasetFilesById}
	              onLoadDatasetFiles={loadDatasetFiles}
	              onUploadDatasetFiles={handleUploadDatasetFiles}
	              onDeleteDatasetFile={handleDeleteDatasetFile}
	            />
	          ) : null}

          {route.kind === "home" ? <ProjectContextCard selectedProject={selectedProject} /> : null}
        </section>
      )}

      {busy ? <p className="subtle">Syncing...</p> : null}
    </div>
  );
}

const root = ReactDOM.createRoot(document.getElementById("app-root"));
root.render(
  <ErrorBoundary>
    <App />
  </ErrorBoundary>
);
