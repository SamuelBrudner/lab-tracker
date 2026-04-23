import * as React from "react";

import { QUESTION_TYPES } from "../../shared/constants.js";

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

export { QuestionPanel };
