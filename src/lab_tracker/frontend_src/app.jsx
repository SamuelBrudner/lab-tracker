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

function formatConfidence(value) {
  if (typeof value !== "number" || Number.isNaN(value)) {
    return "";
  }
  const clamped = Math.max(0, Math.min(1, value));
  return `${Math.round(clamped * 100)}%`;
}

function buildHighlightedSnippet(noteText, candidateText) {
  const note = String(noteText || "");
  const candidate = String(candidateText || "");
  if (!note || !candidate) {
    return null;
  }

  const lowerNote = note.toLowerCase();
  const lowerCandidate = candidate.toLowerCase();

  let matchIndex = lowerNote.indexOf(lowerCandidate);
  let matchLength = candidate.length;

  if (matchIndex < 0 && candidate.endsWith("?")) {
    const trimmed = candidate.slice(0, -1).trim();
    if (trimmed) {
      const lowerTrimmed = trimmed.toLowerCase();
      const trimmedIndex = lowerNote.indexOf(lowerTrimmed);
      if (trimmedIndex >= 0) {
        matchIndex = trimmedIndex;
        matchLength = trimmed.length;
      }
    }
  }

  if (matchIndex < 0) {
    const tokens = candidate.match(/[a-z0-9]{4,}/gi) || [];
    let bestToken = "";
    let bestIndex = -1;
    for (const token of tokens.slice(0, 8)) {
      const tokenIndex = lowerNote.indexOf(token.toLowerCase());
      if (tokenIndex >= 0 && (bestIndex < 0 || tokenIndex < bestIndex)) {
        bestToken = token;
        bestIndex = tokenIndex;
      }
    }
    if (bestIndex >= 0) {
      matchIndex = bestIndex;
      matchLength = bestToken.length;
    }
  }

  if (matchIndex < 0 || matchLength <= 0) {
    return null;
  }

  const contextChars = 80;
  const start = Math.max(0, matchIndex - contextChars);
  const end = Math.min(note.length, matchIndex + matchLength + contextChars);

  const prefix = note.slice(start, matchIndex);
  const match = note.slice(matchIndex, matchIndex + matchLength);
  const suffix = note.slice(matchIndex + matchLength, end);

  return {
    prefix: start > 0 ? `...${prefix}` : prefix,
    match,
    suffix: end < note.length ? `${suffix}...` : suffix,
  };
}

function roleClass(role) {
  return `pill role-${role || "viewer"}`;
}

function sessionTypeClass(sessionType) {
  return `pill session-type session-${sessionType || "scientific"}`;
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
  if (parts.length >= 3 && parts[1] === "sessions" && UUID_RE.test(parts[2] || "")) {
    return { kind: "session", sessionId: parts[2] };
  }
  if (parts.length >= 3 && parts[1] === "datasets" && UUID_RE.test(parts[2] || "")) {
    return { kind: "dataset", datasetId: parts[2] };
  }
  if (parts.length >= 3 && parts[1] === "visualizations" && UUID_RE.test(parts[2] || "")) {
    return { kind: "visualization", vizId: parts[2] };
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
        <div className="item">2. Question capture + extracted candidate staging inbox</div>
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

function QuestionExtractionInboxPanel({
  canWrite,
  busy,
  token,
  selectedProjectId,
  notes,
  questions,
  navigate,
  selectedNoteId,
  onSelectedNoteIdChange,
  note,
  noteRaw,
  candidates,
  onExtractCandidates,
  onUpdateCandidate,
  onToggleCandidateSelected,
  onSelectAllPending,
  onClearSelection,
  onRejectSelected,
  onStageSelected,
}) {
  const sortedNotes = useMemo(() => {
    return [...notes].sort((a, b) => {
      const aTime = new Date(a.created_at || 0).getTime();
      const bTime = new Date(b.created_at || 0).getTime();
      return bTime - aTime;
    });
  }, [notes]);

  const noteText = note ? note.transcribed_text || note.raw_content || "" : "";
  const rawContentType = noteRaw?.content_type || "";
  const hasRawBytes = Boolean(noteRaw?.content_base64);
  const rawIsImage = rawContentType.startsWith("image/");
  const rawImageSrc =
    rawIsImage && hasRawBytes
      ? `data:${rawContentType};base64,${noteRaw.content_base64}`
      : "";

  const totalCount = candidates.length;
  const pendingCount = candidates.filter((item) => item.status === "pending").length;
  const selectedPendingCount = candidates.filter(
    (item) => item.selected && item.status === "pending"
  ).length;
  const stagedCount = candidates.filter((item) => item.status === "staged").length;
  const rejectedCount = candidates.filter((item) => item.status === "rejected").length;

  return (
    <article className="card span-12">
      <div className="item-head">
        <h2>Question Staging Inbox (Extracted)</h2>
        {busy ? <span className="pill">Working...</span> : null}
      </div>
      <p className="subtle">
        Extract question candidates from a note, review suggested metadata, and stage accepted
        questions for later commit.
      </p>

      <form className="form" onSubmit={onExtractCandidates}>
        <label>
          Source note
          <select
            value={selectedNoteId}
            onChange={onSelectedNoteIdChange}
            disabled={!token || !selectedProjectId || notes.length === 0}
          >
            <option value="">Select a note</option>
            {sortedNotes.map((item) => {
              const preview = item.transcribed_text || item.raw_content || "(binary upload)";
              const label = `${formatDate(item.created_at)} · ${preview.slice(0, 80)}`;
              return (
                <option key={item.note_id} value={item.note_id}>
                  {label}
                </option>
              );
            })}
          </select>
        </label>

        <div className="inline">
          <button
            className="btn-primary"
            disabled={!canWrite || !token || !selectedProjectId || !selectedNoteId || busy}
          >
            Extract candidates
          </button>
          <button
            type="button"
            className="btn-secondary"
            disabled={busy || (!selectedNoteId && candidates.length === 0 && !note)}
            onClick={() => onSelectedNoteIdChange({ target: { value: "" } })}
          >
            Clear
          </button>
          {note ? (
            <button
              type="button"
              className="btn-secondary"
              onClick={() => navigate(`/app/notes/${note.note_id}`)}
            >
              Open note detail
            </button>
          ) : null}
        </div>
      </form>

      {!canWrite ? (
        <p className="warn">
          Extraction review requires write access. Ask an admin to provision an editor or admin
          account.
        </p>
      ) : null}

      {note ? (
        <div className="review-layout">
          <section className="review-pane">
            <div className="item-head">
              <h3>Source</h3>
              <div className="inline">
                <span className="pill">{note.status}</span>
                <span className="subtle">{formatDate(note.created_at)}</span>
              </div>
            </div>

            {hasRawBytes ? (
              rawIsImage ? (
                <img className="note-image" src={rawImageSrc} alt="Source note" />
              ) : (
                <div className="item">
                  <div className="item-head">
                    <strong>Raw asset</strong>
                    <span className="subtle">{rawContentType || "unknown type"}</span>
                  </div>
                  <p className="mono">{noteRaw.filename}</p>
                  <p className="mono">{formatBytes(noteRaw.size_bytes)}</p>
                  <a className="link" href={`/notes/${note.note_id}/raw`}>
                    Download
                  </a>
                </div>
              )
            ) : null}

            <div className="stack">
              <div>
                <div className="subtle">Transcribed / raw text</div>
                {noteText ? (
                  <pre className="note-text">{noteText}</pre>
                ) : (
                  <p className="subtle">(No note text available for highlighting.)</p>
                )}
              </div>
              <div className="stack">
                <div className="subtle">Note ID</div>
                <div className="mono">{note.note_id}</div>
              </div>
            </div>
          </section>

          <section className="review-pane">
            <div className="item-head">
              <h3>Candidates</h3>
              <div className="inline">
                <span className="pill" title="total candidates">
                  {totalCount}
                </span>
                <span className="pill" title="pending">
                  pending: {pendingCount}
                </span>
                <span className="pill" title="staged">
                  staged: {stagedCount}
                </span>
                <span className="pill" title="rejected">
                  rejected: {rejectedCount}
                </span>
              </div>
            </div>

            {candidates.length === 0 ? (
              <p className="subtle">
                No candidates loaded. Pick a note and click &quot;Extract candidates&quot;.
              </p>
            ) : (
              <>
                <div className="inline">
                  <button
                    type="button"
                    className="btn-secondary"
                    disabled={busy || candidates.length === 0}
                    onClick={onSelectAllPending}
                  >
                    Select pending
                  </button>
                  <button
                    type="button"
                    className="btn-secondary"
                    disabled={busy || candidates.length === 0}
                    onClick={onClearSelection}
                  >
                    Clear selection
                  </button>
                  <button
                    type="button"
                    className="btn-danger"
                    disabled={!canWrite || busy || selectedPendingCount === 0}
                    onClick={() => onRejectSelected()}
                  >
                    Reject selected
                  </button>
                  <button
                    type="button"
                    className="btn-primary"
                    disabled={!canWrite || busy || selectedPendingCount === 0 || !selectedProjectId}
                    onClick={() => onStageSelected()}
                  >
                    Stage selected ({selectedPendingCount})
                  </button>
                </div>

                <div className="stack">
                  {candidates.map((candidate) => {
                    const snippet = noteText ? buildHighlightedSnippet(noteText, candidate.text) : null;
                    const itemClass = [
                      "item",
                      candidate.status === "rejected" ? "item-rejected" : "",
                      candidate.status === "staged" ? "item-staged" : "",
                      candidate.status === "error" ? "item-error" : "",
                    ]
                      .filter(Boolean)
                      .join(" ");

                    return (
                      <article className={itemClass} key={candidate.local_id}>
                        <div className="item-head">
                          <div className="inline">
                            <input
                              type="checkbox"
                              checked={candidate.selected}
                              disabled={candidate.status !== "pending" || busy}
                              onChange={() => onToggleCandidateSelected(candidate.local_id)}
                              aria-label="Select candidate"
                            />
                            <strong>Candidate</strong>
                            <span className="pill">{formatConfidence(candidate.confidence)}</span>
                            <span className="pill">{candidate.question_type}</span>
                            <span className="pill">{candidate.status}</span>
                          </div>
                          {candidate.staged_question_id ? (
                            <AppLink
                              to={`/app/questions/${candidate.staged_question_id}`}
                              navigate={navigate}
                              className="link"
                            >
                              Open
                            </AppLink>
                          ) : null}
                        </div>

                        {snippet ? (
                          <p className="source-snippet">
                            {snippet.prefix}
                            <mark className="highlight">{snippet.match}</mark>
                            {snippet.suffix}
                          </p>
                        ) : noteText ? (
                          <p className="subtle">Source match not found in note text.</p>
                        ) : null}

                        <div className="form">
                          <label>
                            Question text
                            <textarea
                              value={candidate.text}
                              disabled={candidate.status !== "pending" || !canWrite || busy}
                              onChange={(event) =>
                                onUpdateCandidate(candidate.local_id, { text: event.target.value })
                              }
                            />
                          </label>

                          <div className="review-fields">
                            <label>
                              Type
                              <select
                                value={candidate.question_type}
                                disabled={candidate.status !== "pending" || !canWrite || busy}
                                onChange={(event) =>
                                  onUpdateCandidate(candidate.local_id, {
                                    question_type: event.target.value,
                                  })
                                }
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
                                value={candidate.hypothesis}
                                disabled={candidate.status !== "pending" || !canWrite || busy}
                                onChange={(event) =>
                                  onUpdateCandidate(candidate.local_id, {
                                    hypothesis: event.target.value,
                                  })
                                }
                              />
                            </label>
                          </div>

                          <label>
                            Parent questions (optional)
                            <select
                              multiple
                              value={candidate.parent_question_ids}
                              disabled={candidate.status !== "pending" || !canWrite || busy}
                              onChange={(event) => {
                                const values = Array.from(event.target.selectedOptions).map(
                                  (option) => option.value
                                );
                                onUpdateCandidate(candidate.local_id, {
                                  parent_question_ids: values,
                                });
                              }}
                            >
                              {questions.map((question) => (
                                <option value={question.question_id} key={question.question_id}>
                                  {question.text.slice(0, 80)}
                                </option>
                              ))}
                            </select>
                          </label>

                          {candidate.error ? (
                            <p className="flash error">{candidate.error}</p>
                          ) : null}

                          <div className="inline">
                            <button
                              type="button"
                              className="btn-secondary"
                              disabled={candidate.status !== "pending" || !canWrite || busy}
                              onClick={() => {
                                onRejectSelected([candidate.local_id]);
                              }}
                            >
                              Reject
                            </button>
                            <button
                              type="button"
                              className="btn-primary"
                              disabled={candidate.status !== "pending" || !canWrite || busy}
                              onClick={() => onStageSelected([candidate.local_id])}
                            >
                              Stage
                            </button>
                          </div>
                        </div>
                      </article>
                    );
                  })}
                </div>
              </>
            )}
          </section>
        </div>
      ) : null}
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

function SessionPanel({
  canWrite,
  busy,
  projects,
  selectedProjectId,
  onSelectedProjectChange,
  sessionType,
  onSessionTypeChange,
  sessionPrimaryQuestionId,
  onSessionPrimaryQuestionIdChange,
  activeQuestions,
  questions,
  sessions,
  onCreateSession,
  onCloseSession,
  navigate,
}) {
  const activeSessions = useMemo(() => {
    const items = Array.isArray(sessions)
      ? sessions.filter((session) => session.status === "active")
      : [];
    items.sort((a, b) => {
      const aTime = Date.parse(a.started_at || "") || 0;
      const bTime = Date.parse(b.started_at || "") || 0;
      return bTime - aTime;
    });
    return items;
  }, [sessions]);

  const questionById = useMemo(() => {
    const index = {};
    (questions || []).forEach((question) => {
      index[question.question_id] = question;
    });
    return index;
  }, [questions]);

  const primaryQuestionOptions = useMemo(() => {
    const items = Array.isArray(activeQuestions) ? [...activeQuestions] : [];
    items.sort((a, b) => {
      const aTime = Date.parse(a.updated_at || a.created_at || "") || 0;
      const bTime = Date.parse(b.updated_at || b.created_at || "") || 0;
      return bTime - aTime;
    });
    return items;
  }, [activeQuestions]);

  return (
    <article className="card span-12">
      <div className="item-head">
        <h2>Sessions</h2>
        <span className="pill">{activeSessions.length} active</span>
      </div>
      <p className="subtle">
        Start acquisition sessions (scientific or operational) and share the link code with
        instruments via QR or direct entry.
      </p>

      <form className="form" onSubmit={onCreateSession}>
        <label>
          Project
          <select value={selectedProjectId} onChange={onSelectedProjectChange}>
            <option value="">Select a project</option>
            {projects.map((project) => (
              <option key={project.project_id} value={project.project_id}>
                {project.name}
              </option>
            ))}
          </select>
        </label>

        <label>
          Session type
          <select
            value={sessionType}
            onChange={onSessionTypeChange}
            disabled={!canWrite || !selectedProjectId}
          >
            <option value="scientific">scientific</option>
            <option value="operational">operational</option>
          </select>
        </label>

        {sessionType === "scientific" ? (
          <>
            <label>
              Primary question (required)
              <select
                value={sessionPrimaryQuestionId}
                onChange={onSessionPrimaryQuestionIdChange}
                disabled={!canWrite || !selectedProjectId || primaryQuestionOptions.length === 0}
              >
                <option value="">Select an active question</option>
                {primaryQuestionOptions.map((question) => (
                  <option value={question.question_id} key={question.question_id}>
                    {question.text}
                  </option>
                ))}
              </select>
            </label>
            {primaryQuestionOptions.length === 0 ? (
              <p className="warn">Commit at least one question before starting a scientific session.</p>
            ) : null}
          </>
        ) : null}

        <button
          className="btn-primary"
          disabled={
            !canWrite ||
            !selectedProjectId ||
            busy ||
            (sessionType === "scientific" && !sessionPrimaryQuestionId)
          }
        >
          Start session
        </button>
      </form>

      <h3>Active Sessions</h3>
      {activeSessions.length === 0 ? (
        <p className="subtle">No active sessions for this project.</p>
      ) : (
        <div className="stack">
          {activeSessions.map((session) => {
            const primaryQuestion = session.primary_question_id
              ? questionById[session.primary_question_id] || null
              : null;
            return (
              <article
                key={session.session_id}
                className={`item session-item session-${session.session_type}`}
              >
                <div className="item-head">
                  <AppLink
                    to={`/app/sessions/${session.session_id}`}
                    navigate={navigate}
                    className="link"
                  >
                    <strong>
                      {session.session_type === "scientific" ? "Scientific" : "Operational"} session
                    </strong>
                  </AppLink>
                  <span className={sessionTypeClass(session.session_type)}>{session.session_type}</span>
                </div>

                <div className="inline">
                  <span className="pill">{session.status}</span>
                  <span className="subtle">started {formatDate(session.started_at)}</span>
                </div>

                {session.primary_question_id ? (
                  <p className="subtle">
                    Primary question:{" "}
                    <AppLink
                      to={`/app/questions/${session.primary_question_id}`}
                      navigate={navigate}
                      className="link"
                    >
                      <strong>{primaryQuestion ? primaryQuestion.text : session.primary_question_id}</strong>
                    </AppLink>
                  </p>
                ) : null}

                <div className="stack">
                  <div className="subtle">Link code</div>
                  <div className="mono">{session.link_code}</div>
                  <div className="subtle">Link endpoint</div>
                  <div className="mono">/sessions/by-link/{session.link_code}</div>
                </div>

                <div className="inline">
                  <button
                    type="button"
                    className="btn-secondary"
                    onClick={() => navigate(`/app/sessions/${session.session_id}`)}
                  >
                    View
                  </button>
                  <button
                    type="button"
                    className="btn-danger"
                    disabled={!canWrite || busy}
                    onClick={() => onCloseSession(session.session_id, session.project_id)}
                  >
                    Close session
                  </button>
                </div>
              </article>
            );
          })}
        </div>
      )}
    </article>
  );
}

function AnalysisPanel({
  canWrite,
  busy,
  selectedProjectId,
  datasets,
  analyses,
  visualizations,
  analysisDatasetIds,
  analysisCodeVersion,
  analysisMethodHash,
  analysisEnvironmentHash,
  onAnalysisDatasetIdsChange,
  onAnalysisCodeVersionChange,
  onAnalysisMethodHashChange,
  onAnalysisEnvironmentHashChange,
  onCreateAnalysis,
  onCommitAnalysis,
  onArchiveAnalysis,
  navigate,
}) {
  const [statusFilter, setStatusFilter] = useState("all");

  const datasetsById = useMemo(() => {
    const index = {};
    (datasets || []).forEach((dataset) => {
      index[dataset.dataset_id] = dataset;
    });
    return index;
  }, [datasets]);

  const visualizationsByAnalysisId = useMemo(() => {
    const index = {};
    (visualizations || []).forEach((viz) => {
      const key = viz.analysis_id;
      if (!index[key]) {
        index[key] = [];
      }
      index[key].push(viz);
    });
    Object.values(index).forEach((items) => {
      items.sort((a, b) => {
        const aTime = Date.parse(a.created_at || "") || 0;
        const bTime = Date.parse(b.created_at || "") || 0;
        return bTime - aTime;
      });
    });
    return index;
  }, [visualizations]);

  const datasetOptions = useMemo(() => {
    const items = Array.isArray(datasets) ? [...datasets] : [];
    items.sort((a, b) => {
      const aTime = Date.parse(a.updated_at || a.created_at || "") || 0;
      const bTime = Date.parse(b.updated_at || b.created_at || "") || 0;
      return bTime - aTime;
    });
    return items;
  }, [datasets]);

  const filteredAnalyses = useMemo(() => {
    const items = Array.isArray(analyses) ? [...analyses] : [];
    const scoped =
      statusFilter === "all"
        ? items
        : items.filter((analysis) => analysis.status === statusFilter);
    scoped.sort((a, b) => {
      const aTime = Date.parse(a.updated_at || a.created_at || "") || 0;
      const bTime = Date.parse(b.updated_at || b.created_at || "") || 0;
      return bTime - aTime;
    });
    return scoped;
  }, [analyses, statusFilter]);

  const statusCounts = useMemo(() => {
    const counts = { staged: 0, committed: 0, archived: 0 };
    (analyses || []).forEach((analysis) => {
      if (analysis.status && Object.prototype.hasOwnProperty.call(counts, analysis.status)) {
        counts[analysis.status] += 1;
      }
    });
    return counts;
  }, [analyses]);

  return (
    <article className="card span-12">
      <div className="item-head">
        <h2>Analysis Registry</h2>
        <span className="pill">{(analyses || []).length} total</span>
      </div>
      <p className="subtle">
        Register analyses against datasets, record code + method hashes, and commit once datasets are
        frozen.
      </p>

      <form className="form" onSubmit={onCreateAnalysis}>
        <label>
          Datasets (multi-select)
          <select
            multiple
            value={analysisDatasetIds}
            onChange={onAnalysisDatasetIdsChange}
            disabled={!canWrite || !selectedProjectId || datasetOptions.length === 0}
            size={Math.min(6, Math.max(3, datasetOptions.length))}
          >
            {datasetOptions.map((dataset) => (
              <option value={dataset.dataset_id} key={dataset.dataset_id}>
                {dataset.status} · {dataset.dataset_id}
              </option>
            ))}
          </select>
        </label>
        <label>
          code_version (git commit)
          <input
            value={analysisCodeVersion}
            onChange={onAnalysisCodeVersionChange}
            disabled={!canWrite || !selectedProjectId}
            placeholder="e.g. 4f3c2d1"
          />
        </label>
        <label>
          method_hash
          <input
            value={analysisMethodHash}
            onChange={onAnalysisMethodHashChange}
            disabled={!canWrite || !selectedProjectId}
            placeholder="e.g. sha256 of analysis notebook / params"
          />
        </label>
        <label>
          environment_hash (optional)
          <input
            value={analysisEnvironmentHash}
            onChange={onAnalysisEnvironmentHashChange}
            disabled={!canWrite || !selectedProjectId}
            placeholder="e.g. docker image digest / conda lock hash"
          />
        </label>
        <button
          className="btn-primary"
          disabled={
            !canWrite ||
            !selectedProjectId ||
            busy ||
            analysisDatasetIds.length === 0 ||
            !analysisCodeVersion.trim() ||
            !analysisMethodHash.trim()
          }
        >
          Stage analysis
        </button>
      </form>

      {!canWrite ? (
        <p className="warn">Your role is read-only. Ask an admin/editor to register analyses.</p>
      ) : null}

      <div className="item-head" style={{ marginTop: "0.85rem" }}>
        <h3>Analyses</h3>
        <div className="inline">
          <span className="pill">staged {statusCounts.staged}</span>
          <span className="pill">committed {statusCounts.committed}</span>
          <span className="pill">archived {statusCounts.archived}</span>
        </div>
      </div>

      <label style={{ marginTop: "0.45rem" }}>
        Status filter
        <select value={statusFilter} onChange={(event) => setStatusFilter(event.target.value)}>
          <option value="all">All</option>
          <option value="staged">staged</option>
          <option value="committed">committed</option>
          <option value="archived">archived</option>
        </select>
      </label>

      {filteredAnalyses.length === 0 ? (
        <p className="subtle">No analyses match this filter.</p>
      ) : (
        <div className="stack">
          {filteredAnalyses.map((analysis) => {
            const datasetsForAnalysis = (analysis.dataset_ids || []).map((datasetId) => ({
              datasetId,
              dataset: datasetsById[datasetId] || null,
            }));
            const commitBlocked =
              datasetsForAnalysis.length === 0 ||
              datasetsForAnalysis.some(({ dataset }) => !dataset || dataset.status !== "committed");
            const vizItems = visualizationsByAnalysisId[analysis.analysis_id] || [];
            const canCommit = canWrite && !busy && analysis.status === "staged" && !commitBlocked;
            const canArchive = canWrite && !busy && analysis.status !== "archived";

            return (
              <article key={analysis.analysis_id} className="item">
                <div className="item-head">
                  <strong>{analysis.status}</strong>
                  <span className="subtle">{formatDate(analysis.executed_at)}</span>
                </div>
                <p className="mono">{analysis.analysis_id}</p>
                <p className="mono">code_version: {analysis.code_version}</p>
                <p className="mono">method_hash: {analysis.method_hash}</p>
                <p className="mono">
                  environment_hash: {analysis.environment_hash ? analysis.environment_hash : "(none)"}
                </p>

                <div className="stack">
                  <div className="item">
                    <div className="item-head">
                      <strong>Datasets</strong>
                      <span className="pill">{datasetsForAnalysis.length}</span>
                    </div>
                    {datasetsForAnalysis.length === 0 ? (
                      <p className="subtle">No datasets attached.</p>
                    ) : (
                      <div className="stack">
                        {datasetsForAnalysis.map(({ datasetId, dataset }) => (
                          <div className="item" key={datasetId}>
                            <div className="item-head">
                              <button
                                type="button"
                                className="btn-secondary"
                                onClick={() => navigate(`/app/datasets/${datasetId}`)}
                              >
                                View dataset
                              </button>
                              {dataset ? <span className="pill">{dataset.status}</span> : null}
                            </div>
                            <div className="mono">{datasetId}</div>
                            {dataset ? <div className="mono">commit: {dataset.commit_hash}</div> : null}
                          </div>
                        ))}
                      </div>
                    )}
                  </div>

                  <div className="item">
                    <div className="item-head">
                      <strong>Visualizations</strong>
                      <span className="pill">{vizItems.length}</span>
                    </div>
                    {vizItems.length === 0 ? (
                      <p className="subtle">No visualizations registered for this analysis yet.</p>
                    ) : (
                      <div className="stack">
                        {vizItems.map((viz) => (
                          <article key={viz.viz_id} className="item">
                            <div className="item-head">
                              <AppLink
                                to={`/app/visualizations/${viz.viz_id}`}
                                navigate={navigate}
                                className="link"
                              >
                                <strong>{viz.viz_type}</strong>
                              </AppLink>
                              <span className="subtle">{formatDate(viz.created_at)}</span>
                            </div>
                            <p className="mono">{viz.file_path}</p>
                            {viz.caption ? <p className="subtle">{viz.caption}</p> : null}
                          </article>
                        ))}
                      </div>
                    )}
                  </div>
                </div>

                {analysis.status === "staged" && commitBlocked ? (
                  <p className="warn">
                    Commit requires all linked datasets to be committed.
                  </p>
                ) : null}

                <div className="inline">
                  {analysis.status === "staged" ? (
                    <button
                      type="button"
                      className="btn-primary"
                      disabled={!canCommit}
                      onClick={() => onCommitAnalysis(analysis.analysis_id)}
                    >
                      Commit analysis
                    </button>
                  ) : null}
                  <button
                    type="button"
                    className="btn-secondary"
                    disabled={!canArchive}
                    onClick={() => onArchiveAnalysis(analysis.analysis_id)}
                  >
                    Archive
                  </button>
                </div>
              </article>
            );
          })}
        </div>
      )}
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

function SessionDetailCard({
  token,
  sessionId,
  projects,
  questions,
  navigate,
  onSetActiveProject,
  canWrite,
  onCloseSession,
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [session, setSession] = useState(null);
  const [outputsState, setOutputsState] = useState({ loading: false, error: "", items: [] });
  const [noteState, setNoteState] = useState({ loading: false, error: "", items: [] });

  const project = useMemo(() => {
    if (!session) {
      return null;
    }
    return projects.find((item) => item.project_id === session.project_id) || null;
  }, [projects, session]);

  const primaryQuestion = useMemo(() => {
    if (!session?.primary_question_id) {
      return null;
    }
    return questions.find((item) => item.question_id === session.primary_question_id) || null;
  }, [questions, session]);

  useEffect(() => {
    let canceled = false;
    if (!token || !sessionId) {
      setSession(null);
      setError("");
      return () => {
        canceled = true;
      };
    }
    setBusy(true);
    setError("");
    apiRequest(`/sessions/${sessionId}`, { token })
      .then((payload) => {
        if (!canceled) {
          setSession(payload);
        }
      })
      .catch((err) => {
        if (!canceled) {
          setError(err.message || "Failed to load session.");
          setSession(null);
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
  }, [token, sessionId]);

  useEffect(() => {
    let canceled = false;
    if (!token || !sessionId) {
      setOutputsState({ loading: false, error: "", items: [] });
      return () => {
        canceled = true;
      };
    }

    setOutputsState({ loading: true, error: "", items: [] });
    apiRequest(`/sessions/${sessionId}/outputs?limit=200`, { token })
      .then((items) => {
        if (canceled) {
          return;
        }
        const normalized = Array.isArray(items) ? items : [];
        normalized.sort((a, b) => {
          const aTime = Date.parse(a.created_at || "") || 0;
          const bTime = Date.parse(b.created_at || "") || 0;
          return bTime - aTime;
        });
        setOutputsState({ loading: false, error: "", items: normalized });
      })
      .catch((err) => {
        if (canceled) {
          return;
        }
        setOutputsState({ loading: false, error: err.message || "Failed to load outputs.", items: [] });
      });

    return () => {
      canceled = true;
    };
  }, [sessionId, token]);

  useEffect(() => {
    let canceled = false;
    if (!token || !session) {
      setNoteState({ loading: false, error: "", items: [] });
      return () => {
        canceled = true;
      };
    }

    setNoteState({ loading: true, error: "", items: [] });
    const encodedProjectId = encodeURIComponent(session.project_id);
    apiRequest(`/notes?project_id=${encodedProjectId}&limit=200`, { token })
      .then((items) => {
        if (canceled) {
          return;
        }
        const normalized = Array.isArray(items) ? items : [];
        const linked = normalized.filter((note) => {
          const targets = Array.isArray(note.targets) ? note.targets : [];
          return targets.some(
            (target) => target.entity_type === "session" && target.entity_id === session.session_id
          );
        });
        linked.sort((a, b) => {
          const aTime = Date.parse(a.created_at || "") || 0;
          const bTime = Date.parse(b.created_at || "") || 0;
          return bTime - aTime;
        });
        setNoteState({ loading: false, error: "", items: linked });
      })
      .catch((err) => {
        if (canceled) {
          return;
        }
        setNoteState({ loading: false, error: err.message || "Failed to load linked notes.", items: [] });
      });

    return () => {
      canceled = true;
    };
  }, [session, token]);

  async function handleCloseSession() {
    if (!session || !canWrite) {
      return;
    }
    try {
      const updated = await onCloseSession(session.session_id, session.project_id);
      if (updated) {
        setSession(updated);
      }
    } catch (err) {
      setError(err.message || "Failed to close session.");
    }
  }

  return (
    <article className="card span-8">
      <div className="item-head">
        <h2>Session Detail</h2>
        {busy ? <span className="pill">Loading...</span> : null}
      </div>
      {error ? <p className="flash error">{error}</p> : null}

      {session ? (
        <div className="stack">
          <div className="inline">
            <span className="pill">{session.status}</span>
            <span className={sessionTypeClass(session.session_type)}>{session.session_type}</span>
            {project ? <span className="pill">{project.name}</span> : null}
          </div>

          <div className="stack">
            <div className="subtle">Session ID</div>
            <div className="mono">{session.session_id}</div>
            <div className="subtle">Project ID</div>
            <div className="mono">{session.project_id}</div>
            <div className="subtle">Link code</div>
            <div className="mono">{session.link_code}</div>
            <div className="subtle">Started</div>
            <div className="mono">{formatDate(session.started_at)}</div>
            <div className="subtle">Ended</div>
            <div className="mono">{session.ended_at ? formatDate(session.ended_at) : "(active)"}</div>
          </div>

          {session.primary_question_id ? (
            <div className="stack">
              <div className="subtle">Primary question</div>
              <AppLink
                to={`/app/questions/${session.primary_question_id}`}
                navigate={navigate}
                className="link"
              >
                <strong>{primaryQuestion ? primaryQuestion.text : session.primary_question_id}</strong>
              </AppLink>
              <div className="mono">{session.primary_question_id}</div>
            </div>
          ) : null}

          <div className="stack">
            <div className="item-head">
              <h3>Acquisition Outputs</h3>
              <span className="pill">{outputsState.items.length}</span>
            </div>
            {outputsState.loading ? <p className="subtle">Loading outputs...</p> : null}
            {outputsState.error ? <p className="flash error">{outputsState.error}</p> : null}
            {outputsState.items.length === 0 && !outputsState.loading ? (
              <p className="subtle">(no outputs)</p>
            ) : (
              <div className="stack">
                {outputsState.items.map((output) => (
                  <div className="item" key={output.output_id}>
                    <div className="item-head">
                      <span className="mono">{output.file_path}</span>
                      <span className="subtle">{formatBytes(output.size_bytes)}</span>
                    </div>
                    <p className="mono">sha256: {output.checksum}</p>
                    <p className="subtle">{formatDate(output.created_at)}</p>
                  </div>
                ))}
              </div>
            )}
          </div>

          <div className="stack">
            <div className="item-head">
              <h3>Linked Notes</h3>
              <span className="pill">{noteState.items.length}</span>
            </div>
            {noteState.loading ? <p className="subtle">Loading linked notes...</p> : null}
            {noteState.error ? <p className="flash error">{noteState.error}</p> : null}
            {noteState.items.length === 0 && !noteState.loading ? (
              <p className="subtle">(no linked notes)</p>
            ) : (
              <div className="stack">
                {noteState.items.map((note) => {
                  const preview = note.transcribed_text || note.raw_content || "(binary upload)";
                  return (
                    <div className="item" key={note.note_id}>
                      <div className="item-head">
                        <AppLink to={`/app/notes/${note.note_id}`} navigate={navigate} className="link">
                          <strong>{preview.slice(0, 120)}</strong>
                        </AppLink>
                        <span className="pill">{note.status}</span>
                      </div>
                      <p className="mono">{note.note_id}</p>
                      <p className="subtle">{formatDate(note.created_at)}</p>
                    </div>
                  );
                })}
              </div>
            )}
          </div>
        </div>
      ) : null}

      <div className="inline detail-actions">
        <button type="button" className="btn-secondary" onClick={() => navigate("/app")}>
          Back
        </button>
        {session ? (
          <button
            type="button"
            className="btn-primary"
            onClick={() => {
              onSetActiveProject(session.project_id);
              navigate("/app");
            }}
          >
            Set active project
          </button>
        ) : null}
        {session && session.status === "active" ? (
          <button
            type="button"
            className="btn-danger"
            disabled={!canWrite}
            onClick={handleCloseSession}
          >
            Close session
          </button>
        ) : null}
      </div>
    </article>
  );
}

function DatasetDetailCard({ token, datasetId, projects, navigate, onSetActiveProject }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [dataset, setDataset] = useState(null);
  const [fileState, setFileState] = useState({ loading: false, error: "", items: [] });

  const project = useMemo(() => {
    if (!dataset) {
      return null;
    }
    return projects.find((item) => item.project_id === dataset.project_id) || null;
  }, [projects, dataset]);

  useEffect(() => {
    let canceled = false;
    if (!token || !datasetId) {
      setDataset(null);
      setError("");
      return () => {
        canceled = true;
      };
    }
    setBusy(true);
    setError("");
    apiRequest(`/datasets/${datasetId}`, { token })
      .then((payload) => {
        if (!canceled) {
          setDataset(payload);
        }
      })
      .catch((err) => {
        if (!canceled) {
          setError(err.message || "Failed to load dataset.");
          setDataset(null);
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
  }, [token, datasetId]);

  useEffect(() => {
    let canceled = false;
    if (!token || !dataset || dataset.status === "committed") {
      setFileState({ loading: false, error: "", items: [] });
      return () => {
        canceled = true;
      };
    }

    setFileState((current) => ({ ...current, loading: true, error: "" }));
    apiRequest(`/datasets/${dataset.dataset_id}/files?limit=200`, { token })
      .then((items) => {
        if (canceled) {
          return;
        }
        setFileState({ loading: false, error: "", items: Array.isArray(items) ? items : [] });
      })
      .catch((err) => {
        if (canceled) {
          return;
        }
        setFileState({ loading: false, error: err.message || "Failed to load dataset files.", items: [] });
      });

    return () => {
      canceled = true;
    };
  }, [dataset, token]);

  const committedFiles = dataset?.commit_manifest?.files || [];
  const stagedFiles = fileState.items || [];
  const fileItems = dataset?.status === "committed" ? committedFiles : stagedFiles;

  return (
    <article className="card span-8">
      <div className="item-head">
        <h2>Dataset Detail</h2>
        {busy ? <span className="pill">Loading...</span> : null}
      </div>
      {error ? <p className="flash error">{error}</p> : null}

      {dataset ? (
        <div className="stack">
          <div className="inline">
            <span className="pill">{dataset.status}</span>
            {project ? <span className="pill">{project.name}</span> : null}
          </div>

          <div className="stack">
            <div className="subtle">Dataset ID</div>
            <div className="mono">{dataset.dataset_id}</div>
            <div className="subtle">Project ID</div>
            <div className="mono">{dataset.project_id}</div>
            <div className="subtle">Commit hash</div>
            <div className="mono">{dataset.commit_hash}</div>
            <div className="subtle">Created</div>
            <div className="mono">{formatDate(dataset.created_at)}</div>
            <div className="subtle">Updated</div>
            <div className="mono">{formatDate(dataset.updated_at)}</div>
          </div>

          <div className="stack">
            <div className="subtle">Question links</div>
            {(dataset.question_links || []).length === 0 ? (
              <p className="subtle">(none)</p>
            ) : (
              <div className="stack">
                {(dataset.question_links || []).map((link) => (
                  <div className="item" key={`${link.role}:${link.question_id}`}>
                    <div className="item-head">
                      <AppLink
                        to={`/app/questions/${link.question_id}`}
                        navigate={navigate}
                        className="link"
                      >
                        <strong>{link.role}</strong>
                      </AppLink>
                      <span className="pill">{link.outcome_status}</span>
                    </div>
                    <p className="mono">{link.question_id}</p>
                  </div>
                ))}
              </div>
            )}
          </div>

          <div className="stack">
            <div className="item-head">
              <h3>Files</h3>
              <span className="pill">
                {dataset.status === "committed" ? "committed" : "staged"} {fileItems.length}
              </span>
            </div>
            {dataset.status !== "committed" && fileState.loading ? (
              <p className="subtle">Loading attached files...</p>
            ) : null}
            {dataset.status !== "committed" && fileState.error ? (
              <p className="flash error">{fileState.error}</p>
            ) : null}
            {fileItems.length === 0 ? (
              <p className="subtle">(no files)</p>
            ) : (
              <div className="stack">
                {fileItems.map((file) => (
                  <div className="item" key={file.file_id || file.path}>
                    <div className="item-head">
                      <span className="mono">{file.path}</span>
                      <span className="subtle">{formatBytes(file.size_bytes)}</span>
                    </div>
                    <p className="mono">sha256: {file.checksum}</p>
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
      ) : null}

      <div className="inline detail-actions">
        <button type="button" className="btn-secondary" onClick={() => navigate("/app")}>
          Back
        </button>
        {dataset ? (
          <button
            type="button"
            className="btn-primary"
            onClick={() => {
              onSetActiveProject(dataset.project_id);
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

function VisualizationDetailCard({ token, vizId, navigate }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [viz, setViz] = useState(null);

  useEffect(() => {
    let canceled = false;
    if (!token || !vizId) {
      setViz(null);
      setError("");
      return () => {
        canceled = true;
      };
    }
    setBusy(true);
    setError("");
    apiRequest(`/visualizations/${vizId}`, { token })
      .then((payload) => {
        if (!canceled) {
          setViz(payload);
        }
      })
      .catch((err) => {
        if (!canceled) {
          setError(err.message || "Failed to load visualization.");
          setViz(null);
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
  }, [token, vizId]);

  return (
    <article className="card span-8">
      <div className="item-head">
        <h2>Visualization Detail</h2>
        {busy ? <span className="pill">Loading...</span> : null}
      </div>
      {error ? <p className="flash error">{error}</p> : null}

      {viz ? (
        <div className="stack">
          <div className="inline">
            <span className="pill">{viz.viz_type}</span>
          </div>
          <div className="stack">
            <div className="subtle">Visualization ID</div>
            <div className="mono">{viz.viz_id}</div>
            <div className="subtle">Analysis ID</div>
            <div className="mono">{viz.analysis_id}</div>
            <div className="subtle">File path</div>
            <div className="mono">{viz.file_path}</div>
            <div className="subtle">Caption</div>
            <div>{viz.caption || <span className="subtle">(none)</span>}</div>
            <div className="subtle">Related claim IDs</div>
            {(viz.related_claim_ids || []).length === 0 ? (
              <div className="subtle">(none)</div>
            ) : (
              <div className="stack">
                {(viz.related_claim_ids || []).map((claimId) => (
                  <div className="mono" key={claimId}>
                    {claimId}
                  </div>
                ))}
              </div>
            )}
            <div className="subtle">Created</div>
            <div className="mono">{formatDate(viz.created_at)}</div>
            <div className="subtle">Updated</div>
            <div className="mono">{formatDate(viz.updated_at)}</div>
          </div>
        </div>
      ) : null}

      <div className="inline detail-actions">
        <button type="button" className="btn-secondary" onClick={() => navigate("/app")}>
          Back
        </button>
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
  const [sessions, setSessions] = useState([]);
  const [analyses, setAnalyses] = useState([]);
  const [visualizations, setVisualizations] = useState([]);

  const [extractionNoteId, setExtractionNoteId] = useState("");
  const [extractionNote, setExtractionNote] = useState(null);
  const [extractionNoteRaw, setExtractionNoteRaw] = useState(null);
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

  const [sessionType, setSessionType] = useState("scientific");
  const [sessionPrimaryQuestionId, setSessionPrimaryQuestionId] = useState("");

  const [analysisDatasetIds, setAnalysisDatasetIds] = useState([]);
  const [analysisCodeVersion, setAnalysisCodeVersion] = useState("");
  const [analysisMethodHash, setAnalysisMethodHash] = useState("");
  const [analysisEnvironmentHash, setAnalysisEnvironmentHash] = useState("");

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
      setSessions([]);
      setAnalyses([]);
      setVisualizations([]);
    }
  }

  async function refreshProjectData(projectId) {
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
    setAnalysisDatasetIds([]);
    setSessionPrimaryQuestionId("");
  }, [selectedProjectId, token]);

  useEffect(() => {
    setExtractionNoteId("");
    setExtractionNote(null);
    setExtractionNoteRaw(null);
    setExtractionCandidates([]);
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

  function handleExtractionNoteIdChange(event) {
    const nextValue = event?.target?.value || "";
    setExtractionNoteId(nextValue);
    setExtractionNote(null);
    setExtractionNoteRaw(null);
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
        } catch (err) {
          setExtractionNoteRaw(null);
        }
      } else {
        setExtractionNoteRaw(null);
      }

      const payload = await apiRequest(`/notes/${extractionNoteId}/extract-questions`, {
        method: "POST",
        token,
        body: {},
      });
      const extracted = Array.isArray(payload) ? payload : [];
      const batchId = Date.now().toString(36);
      setExtractionCandidates(
        extracted.map((item, index) => ({
          local_id: `${batchId}-${index}`,
          selected: true,
          status: "pending",
          text: String(item.text || ""),
          confidence: typeof item.confidence === "number" ? item.confidence : null,
          question_type: item.suggested_question_type || "other",
          hypothesis: "",
          parent_question_ids: [],
          provenance: item.provenance || "",
          staged_question_id: "",
          error: "",
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
        return { ...item, status: "rejected", selected: false, error: "" };
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
            ? { ...item, status: "error", error: "Question text is required." }
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
            method: "POST",
            token,
            body: {
              project_id: selectedProjectId,
              text: String(candidate.text || "").trim(),
              question_type: candidate.question_type || "other",
              hypothesis: candidate.hypothesis.trim() || null,
              parent_question_ids:
                candidate.parent_question_ids && candidate.parent_question_ids.length > 0
                  ? candidate.parent_question_ids
                  : null,
              status: "staged",
              created_from: "meeting_capture",
              created_by: candidate.provenance || null,
            },
          })
        )
      );

      results.forEach((result, index) => {
        const candidate = toStage[index];
        if (result.status === "fulfilled" && result.value) {
          const created = result.value;
          createdQuestionIds.push(created.question_id);
          updatesById.set(candidate.local_id, {
            status: "staged",
            selected: false,
            staged_question_id: created.question_id,
            error: "",
          });
          return;
        }
        const message =
          result.status === "rejected"
            ? result.reason?.message || String(result.reason || "Failed to stage candidate.")
            : "Failed to stage candidate.";
        updatesById.set(candidate.local_id, {
          status: "error",
          error: message,
        });
      });

      setExtractionCandidates((current) =>
        current.map((item) => (updatesById.has(item.local_id) ? { ...item, ...updatesById.get(item.local_id) } : item))
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
          nextTargets.push({ entity_type: "question", entity_id: questionId });
        }
        if (nextTargets.length !== existingTargets.length) {
          const updatedNote = await apiRequest(`/notes/${extractionNote.note_id}`, {
            method: "PATCH",
            token,
            body: { targets: nextTargets },
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
        method: "POST",
        token,
        body,
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
        method: "PATCH",
        token,
        body: { status: "closed", ended_at: new Date().toISOString() },
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
        method: "POST",
        token,
        body: {
          project_id: selectedProjectId,
          dataset_ids: analysisDatasetIds,
          code_version: analysisCodeVersion.trim(),
          method_hash: analysisMethodHash.trim(),
          environment_hash: analysisEnvironmentHash.trim() || null,
        },
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
        method: "POST",
        token,
        body: {},
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
        method: "PATCH",
        token,
        body: { status: "archived" },
      });
      await refreshProjectData(selectedProjectId);
      setFlash("Analysis archived.");
    } catch (err) {
      setFlash("", err.message || "Failed to archive analysis.");
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

          {route.kind === "home" ? (
            <SessionPanel
              canWrite={canWrite}
              busy={busy}
              projects={projects}
              selectedProjectId={selectedProjectId}
              onSelectedProjectChange={(event) => setSelectedProjectId(event.target.value)}
              sessionType={sessionType}
              onSessionTypeChange={(event) => setSessionType(event.target.value)}
              sessionPrimaryQuestionId={sessionPrimaryQuestionId}
              onSessionPrimaryQuestionIdChange={(event) =>
                setSessionPrimaryQuestionId(event.target.value)
              }
              activeQuestions={activeQuestions}
              questions={questions}
              sessions={sessions}
              onCreateSession={handleCreateSession}
              onCloseSession={handleCloseSession}
              navigate={navigate}
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

          {route.kind === "session" ? (
            <SessionDetailCard
              token={token}
              sessionId={route.sessionId}
              projects={projects}
              questions={questions}
              navigate={navigate}
              onSetActiveProject={(projectId) => setSelectedProjectId(projectId)}
              canWrite={canWrite}
              onCloseSession={handleCloseSession}
            />
          ) : null}

          {route.kind === "dataset" ? (
            <DatasetDetailCard
              token={token}
              datasetId={route.datasetId}
              projects={projects}
              navigate={navigate}
              onSetActiveProject={(projectId) => setSelectedProjectId(projectId)}
            />
          ) : null}

          {route.kind === "visualization" ? (
            <VisualizationDetailCard token={token} vizId={route.vizId} navigate={navigate} />
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

          {route.kind === "home" ? (
            <AnalysisPanel
              canWrite={canWrite}
              busy={busy}
              selectedProjectId={selectedProjectId}
              datasets={datasets}
              analyses={analyses}
              visualizations={visualizations}
              analysisDatasetIds={analysisDatasetIds}
              analysisCodeVersion={analysisCodeVersion}
              analysisMethodHash={analysisMethodHash}
              analysisEnvironmentHash={analysisEnvironmentHash}
              onAnalysisDatasetIdsChange={(event) => {
                const selected = Array.from(event.target.selectedOptions || []).map(
                  (option) => option.value
                );
                setAnalysisDatasetIds(selected);
              }}
              onAnalysisCodeVersionChange={(event) => setAnalysisCodeVersion(event.target.value)}
              onAnalysisMethodHashChange={(event) => setAnalysisMethodHash(event.target.value)}
              onAnalysisEnvironmentHashChange={(event) =>
                setAnalysisEnvironmentHash(event.target.value)
              }
              onCreateAnalysis={handleCreateAnalysis}
              onCommitAnalysis={handleCommitAnalysis}
              onArchiveAnalysis={handleArchiveAnalysis}
              navigate={navigate}
            />
          ) : null}

          {route.kind === "home" ? (
            <QuestionExtractionInboxPanel
              canWrite={canWrite}
              busy={busy}
              token={token}
              selectedProjectId={selectedProjectId}
              notes={notes}
              questions={questions}
              navigate={navigate}
              selectedNoteId={extractionNoteId}
              onSelectedNoteIdChange={handleExtractionNoteIdChange}
              note={extractionNote}
              noteRaw={extractionNoteRaw}
              candidates={extractionCandidates}
              onExtractCandidates={handleExtractQuestionCandidates}
              onUpdateCandidate={handleUpdateExtractionCandidate}
              onToggleCandidateSelected={handleToggleExtractionCandidateSelected}
              onSelectAllPending={handleSelectAllPendingCandidates}
              onClearSelection={handleClearCandidateSelection}
              onRejectSelected={handleRejectExtractionCandidates}
              onStageSelected={handleStageExtractionCandidates}
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
