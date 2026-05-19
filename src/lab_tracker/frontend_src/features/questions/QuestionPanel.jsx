import * as React from "react";

import { QUESTION_TYPES } from "../../shared/constants.js";

function getParentQuestionIds(question) {
  return Array.isArray(question.parent_question_ids) ? question.parent_question_ids : [];
}

function compareQuestions(left, right) {
  const leftCreated = left.created_at || "";
  const rightCreated = right.created_at || "";
  if (leftCreated !== rightCreated) {
    return leftCreated.localeCompare(rightCreated);
  }
  return left.text.localeCompare(right.text);
}

function buildQuestionHierarchy(questions) {
  const questionById = new Map(questions.map((question) => [question.question_id, question]));
  const childrenByParentId = new Map();
  const childIdsWithParents = new Set();

  questions.forEach((question) => {
    getParentQuestionIds(question).forEach((parentId) => {
      if (!questionById.has(parentId)) {
        return;
      }
      childIdsWithParents.add(question.question_id);
      const children = childrenByParentId.get(parentId) || [];
      children.push(question);
      childrenByParentId.set(parentId, children);
    });
  });

  childrenByParentId.forEach((children) => children.sort(compareQuestions));

  const roots = questions
    .filter((question) => !childIdsWithParents.has(question.question_id))
    .sort(compareQuestions);
  return { childrenByParentId, questionById, roots };
}

function ParentSummary({ question, questionById }) {
  const parentNames = getParentQuestionIds(question)
    .map((parentId) => questionById.get(parentId)?.text || parentId)
    .filter(Boolean);

  if (parentNames.length === 0) {
    return null;
  }

  return <p className="subtle question-parent-list">Parents: {parentNames.join(" / ")}</p>;
}

function QuestionMapNode({
  childrenByParentId,
  depth,
  navigate,
  path,
  question,
  questionById,
}) {
  if (path.has(question.question_id)) {
    return (
      <div className="question-node" style={{ marginLeft: `${depth * 0.85}rem` }}>
        <span className="pill">cycle hidden</span>
      </div>
    );
  }

  const nextPath = new Set(path);
  nextPath.add(question.question_id);
  const children = childrenByParentId.get(question.question_id) || [];
  const validParentCount = getParentQuestionIds(question).filter((parentId) =>
    questionById.has(parentId)
  ).length;
  const isSuperseded = question.status === "superseded";
  const replacement = question.superseded_by_question_id
    ? questionById.get(question.superseded_by_question_id)
    : null;

  return (
    <div
      className={`question-node${isSuperseded ? " question-node-superseded" : ""}`}
      style={{ marginLeft: `${depth * 0.85}rem` }}
    >
      <div className="question-node-row">
        <button
          className="link question-node-text"
          type="button"
          onClick={() => navigate(`/app/questions/${question.question_id}`)}
        >
          {question.text}
        </button>
        <span className="pill">{question.question_type}</span>
        <span className="pill">{question.status}</span>
        {validParentCount > 1 ? <span className="pill">also linked elsewhere</span> : null}
        {replacement ? (
          <button
            className="link"
            type="button"
            onClick={() => navigate(`/app/questions/${replacement.question_id}`)}
          >
            superseded by {replacement.text}
          </button>
        ) : null}
      </div>
      <ParentSummary question={question} questionById={questionById} />
      {children.length > 0 ? (
        <div className="question-node-children">
          {children.map((child) => (
            <QuestionMapNode
              childrenByParentId={childrenByParentId}
              depth={depth + 1}
              key={`${question.question_id}:${child.question_id}`}
              navigate={navigate}
              path={nextPath}
              question={child}
              questionById={questionById}
            />
          ))}
        </div>
      ) : null}
    </div>
  );
}

function QuestionMap({ navigate, questions }) {
  const { childrenByParentId, questionById, roots } = buildQuestionHierarchy(questions);

  return (
    <>
      <h3>Question Map</h3>
      {questions.length === 0 ? (
        <p className="subtle">No questions to map yet.</p>
      ) : (
        <div className="question-map" data-testid="question-map">
          {roots.map((question) => (
            <QuestionMapNode
              childrenByParentId={childrenByParentId}
              depth={0}
              key={question.question_id}
              navigate={navigate}
              path={new Set()}
              question={question}
              questionById={questionById}
            />
          ))}
        </div>
      )}
    </>
  );
}

function QuestionPanel({
  canWrite,
  busy,
  selectedProjectId,
  questionText,
  questionType,
  questionHypothesis,
  questionParentIds = [],
  onQuestionTextChange,
  onQuestionTypeChange,
  onQuestionHypothesisChange,
  onQuestionParentIdsChange,
  onCreateQuestion,
  questions = [],
  stagedQuestions = [],
  onActivateQuestion,
  navigate = () => {},
}) {
  const { questionById } = buildQuestionHierarchy(questions);
  const parentSelectSize = Math.max(3, Math.min(6, questions.length || 3));

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
        <label>
          Parent questions
          <select
            multiple
            size={parentSelectSize}
            value={questionParentIds}
            onChange={onQuestionParentIdsChange}
            disabled={!canWrite || !selectedProjectId || questions.length === 0}
          >
            {questions.map((question) => (
              <option value={question.question_id} key={question.question_id}>
                {question.text}
              </option>
            ))}
          </select>
        </label>
        <p className="subtle">
          Optional. Select broad motivating questions that this new question should sit under.
        </p>
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
              <ParentSummary question={question} questionById={questionById} />
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

      <QuestionMap navigate={navigate} questions={questions} />
    </article>
  );
}

export { QuestionPanel };
