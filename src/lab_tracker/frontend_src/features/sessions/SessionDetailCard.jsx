import * as React from "react";

import { buildApiPath, fetchAllPages } from "../../shared/api.js";
import { formatBytes, formatDate, sessionTypeClass } from "../../shared/formatters.js";
import { AppLink } from "../../shared/routing.jsx";
import { useApiResource } from "../../hooks/useApiResource.js";

const { useEffect, useMemo, useState } = React;

function SessionDetailCard({
  token,
  sessionId,
  projects,
  questions,
  navigate,
  onSetActiveProject,
  canWrite,
  onCloseSession,
  onPromoteSession,
}) {
  const { data: session, error: loadError, loading } = useApiResource(
    token && sessionId ? `/sessions/${sessionId}` : "",
    token,
    "Failed to load session."
  );
  const [actionError, setActionError] = useState("");
  const [outputsState, setOutputsState] = useState({ loading: false, error: "", items: [] });
  const [noteState, setNoteState] = useState({ loading: false, error: "", items: [] });
  const [promotionQuestionId, setPromotionQuestionId] = useState("");
  const [promotionBusy, setPromotionBusy] = useState(false);

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

  const promotionOptions = useMemo(() => {
    if (!session) {
      return [];
    }
    const items = (questions || [])
      .filter((question) => question.project_id === session.project_id)
      .filter((question) => question.status === "active");
    items.sort((a, b) => {
      const aTime = Date.parse(a.updated_at || a.created_at || "") || 0;
      const bTime = Date.parse(b.updated_at || b.created_at || "") || 0;
      return bTime - aTime;
    });
    return items;
  }, [questions, session]);

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

  useEffect(() => {
    let canceled = false;
    if (!token || !sessionId) {
      setOutputsState({ loading: false, error: "", items: [] });
      return () => {
        canceled = true;
      };
    }

    setOutputsState({ loading: true, error: "", items: [] });
    fetchAllPages(`/sessions/${sessionId}/outputs`, { token })
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
        if (!canceled) {
          setOutputsState({
            loading: false,
            error: err.message || "Failed to load outputs.",
            items: [],
          });
        }
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
    fetchAllPages(
      buildApiPath("/notes", {
        project_id: session.project_id,
        target_entity_type: "session",
        target_entity_id: session.session_id,
      }),
      { token }
    )
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
        setNoteState({ loading: false, error: "", items: normalized });
      })
      .catch((err) => {
        if (!canceled) {
          setNoteState({
            loading: false,
            error: err.message || "Failed to load linked notes.",
            items: [],
          });
        }
      });

    return () => {
      canceled = true;
    };
  }, [session, token]);

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
                  disabled={!canWrite || promotionBusy || promotionOptions.length === 0}
                >
                  <option value="">Select an active question</option>
                  {promotionOptions.map((question) => (
                    <option value={question.question_id} key={question.question_id}>
                      {question.text}
                    </option>
                  ))}
                </select>
              </label>
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

export { SessionDetailCard };
