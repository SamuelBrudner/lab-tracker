import * as React from "react";

import { formatDate } from "../../shared/formatters.js";
import { useApiResource } from "../../hooks/useApiResource.js";

const { useMemo } = React;

function QuestionDetailCard({ token, questionId, projects, navigate, onSetActiveProject }) {
  const { data: question, error, loading } = useApiResource(
    token && questionId ? `/questions/${questionId}` : "",
    token,
    "Failed to load question."
  );

  const project = useMemo(() => {
    if (!question) {
      return null;
    }
    return projects.find((item) => item.project_id === question.project_id) || null;
  }, [projects, question]);

  return (
    <article className="card span-8">
      <div className="item-head">
        <h2>Question Detail</h2>
        {loading ? <span className="pill">Loading...</span> : null}
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

export { QuestionDetailCard };
