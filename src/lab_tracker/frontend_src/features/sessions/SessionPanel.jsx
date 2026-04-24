import * as React from "react";

import { formatDate, sessionTypeClass } from "../../shared/formatters.js";
import { AppLink } from "../../shared/routing.jsx";

const { useMemo } = React;

function SessionPanel({
  canWrite,
  busy,
  loading,
  error,
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
      {loading ? <p className="subtle">Loading active sessions...</p> : null}
      {error ? <p className="flash error">{error}</p> : null}
      {!loading && activeSessions.length === 0 ? (
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

export { SessionPanel };
