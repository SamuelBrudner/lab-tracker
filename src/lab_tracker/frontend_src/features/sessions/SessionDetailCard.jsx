import * as React from "react";

import { formatDate, sessionTypeClass } from "../../shared/formatters.js";
import { AppLink } from "../../shared/routing.jsx";
import { SessionLinkedNotesSection } from "./SessionLinkedNotesSection.jsx";
import { SessionOutputsSection } from "./SessionOutputsSection.jsx";
import { useSessionDetailData } from "./useSessionDetailData.js";

const { useEffect, useMemo, useState } = React;

function SessionDetailCard({
  token,
  sessionId,
  projects,
  navigate,
  onSetActiveProject,
  canWrite,
  onCloseSession,
  onPromoteSession,
}) {
  const [actionError, setActionError] = useState("");
  const [promotionQuestionId, setPromotionQuestionId] = useState("");
  const [promotionBusy, setPromotionBusy] = useState(false);
  const {
    activeQuestionState,
    loadError,
    loading,
    noteState,
    outputsState,
    primaryQuestion,
    project,
    session,
  } = useSessionDetailData({
    token,
    sessionId,
    projects,
  });

  const promotionOptions = useMemo(() => {
    const items = [...(activeQuestionState.items || [])];
    items.sort((a, b) => {
      const aTime = Date.parse(a.updated_at || a.created_at || "") || 0;
      const bTime = Date.parse(b.updated_at || b.created_at || "") || 0;
      return bTime - aTime;
    });
    return items;
  }, [activeQuestionState.items]);

  useEffect(() => {
    setActionError("");
  }, [sessionId]);

  useEffect(() => {
    if (!session || session.session_type !== "operational") {
      setPromotionQuestionId("");
      return;
    }
    setPromotionQuestionId((current) => current || promotionOptions[0]?.question_id || "");
  }, [promotionOptions, session]);

  async function handleCloseSession() {
    if (!session || !canWrite) {
      return;
    }
    setActionError("");
    try {
      const updated = await onCloseSession(session.session_id, session.project_id);
      if (!updated) {
        setActionError("Failed to close session.");
      }
    } catch (err) {
      setActionError(err.message || "Failed to close session.");
    }
  }

  async function handlePromoteSession() {
    if (!session || !canWrite || !onPromoteSession) {
      return;
    }
    if (!promotionQuestionId) {
      setActionError("Pick a primary question to promote this session.");
      return;
    }
    setPromotionBusy(true);
    setActionError("");
    try {
      const updated = await onPromoteSession(
        session.session_id,
        promotionQuestionId,
        session.project_id
      );
      if (!updated) {
        setActionError("Failed to promote session.");
      }
    } catch (err) {
      setActionError(err.message || "Failed to promote session.");
    } finally {
      setPromotionBusy(false);
    }
  }

  const error = actionError || loadError;

  return (
    <article className="card span-8">
      <div className="item-head">
        <h2>Session Detail</h2>
        {loading ? <span className="pill">Loading...</span> : null}
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

          {session.session_type === "operational" ? (
            <div className="stack">
              <div className="item-head">
                <h3>Promotion</h3>
              </div>
              <p className="subtle">
                Promote this operational session into the scientific knowledge graph by linking a primary
                question.
              </p>
              <label>
                Primary question (required)
                <select
                  value={promotionQuestionId}
                  onChange={(event) => setPromotionQuestionId(event.target.value)}
                  disabled={
                    !canWrite ||
                    promotionBusy ||
                    activeQuestionState.loading ||
                    promotionOptions.length === 0
                  }
                >
                  <option value="">Select an active question</option>
                  {promotionOptions.map((question) => (
                    <option value={question.question_id} key={question.question_id}>
                      {question.text}
                    </option>
                  ))}
                </select>
              </label>
              {activeQuestionState.error ? (
                <p className="flash error">{activeQuestionState.error}</p>
              ) : null}
              {promotionOptions.length === 0 ? (
                <p className="warn">Activate a question in this project before promoting the session.</p>
              ) : null}
              <button
                type="button"
                className="btn-primary"
                disabled={!canWrite || promotionBusy || promotionOptions.length === 0 || !promotionQuestionId}
                onClick={handlePromoteSession}
              >
                Promote to scientific
              </button>
            </div>
          ) : null}

          <SessionOutputsSection outputsState={outputsState} />

          <SessionLinkedNotesSection noteState={noteState} navigate={navigate} />
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

export { SessionDetailCard };
