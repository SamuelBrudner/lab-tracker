import * as React from "react";

import { QUESTION_TYPES } from "../../shared/constants.js";
import { apiRequest, buildApiPath } from "../../shared/api.js";
import { formatDate } from "../../shared/formatters.js";
import { useApiResource } from "../../hooks/useApiResource.js";
import { useProjectAccess } from "../../hooks/useProjectAccess.js";

const { useEffect, useMemo, useState } = React;

function getParentQuestionIds(question) {
  return Array.isArray(question?.parent_question_ids) ? question.parent_question_ids : [];
}

function compactNoteLabel(note) {
  const content = note.transcribed_text || note.raw_content || note.raw_asset?.filename || note.note_id;
  return content.length > 88 ? `${content.slice(0, 85)}...` : content;
}

function QuestionDetailCard({
  token,
  questionId,
  projects,
  questions = [],
  navigate,
  onSetActiveProject,
  canWrite: dashboardCanWrite = false,
  user = null,
  setBusy = () => {},
  setFlash = () => {},
}) {
  const { data: question, error, loading } = useApiResource(
    token && questionId ? `/questions/${questionId}` : "",
    token,
    "Failed to load question."
  );
  // Key write access to the loaded question's own project, not the dashboard
  // selection; fall back to the passed permission until the project is known.
  const questionAccess = useProjectAccess(question?.project_id, {
    token,
    user,
    enabled: Boolean(question?.project_id),
  });
  const canWrite = question?.project_id ? questionAccess.canContribute : dashboardCanWrite;
  const { data: projectQuestions } = useApiResource(
    token && question?.project_id
      ? buildApiPath("/questions", {
          project_id: question.project_id,
          limit: 200,
          offset: 0,
        })
      : "",
    token,
    "Failed to load project questions."
  );
  const { data: targetedNotes } = useApiResource(
    token && question?.project_id
      ? buildApiPath("/notes", {
          project_id: question.project_id,
          target_entity_type: "question",
          target_entity_id: question.question_id,
          limit: 200,
          offset: 0,
        })
      : "",
    token,
    "Failed to load targeted notes."
  );
  const { data: refactors } = useApiResource(
    token && questionId
      ? buildApiPath(`/questions/${questionId}/refactors`, { limit: 50, offset: 0 })
      : "",
    token,
    "Failed to load question refactors."
  );
  const [refactorOpen, setRefactorOpen] = useState(false);
  const [form, setForm] = useState({
    childQuestionIds: [],
    hypothesis: "",
    noteIds: [],
    parentIds: [],
    reason: "",
    status: "staged",
    text: "",
    type: "descriptive",
  });

  const allQuestions = useMemo(() => {
    if (Array.isArray(projectQuestions) && projectQuestions.length > 0) {
      return projectQuestions;
    }
    return questions;
  }, [projectQuestions, questions]);
  const questionById = useMemo(
    () => new Map(allQuestions.map((item) => [item.question_id, item])),
    [allQuestions]
  );
  const project = useMemo(() => {
    if (!question) {
      return null;
    }
    return projects.find((item) => item.project_id === question.project_id) || null;
  }, [projects, question]);
  const parentQuestionNames = useMemo(
    () =>
      getParentQuestionIds(question).map(
        (parentId) => questionById.get(parentId)?.text || parentId
      ),
    [question, questionById]
  );
  const directChildren = useMemo(
    () =>
      allQuestions.filter((item) =>
        getParentQuestionIds(item).includes(question?.question_id || "")
      ),
    [allQuestions, question]
  );
  const parentOptions = useMemo(
    () => allQuestions.filter((item) => item.question_id !== question?.question_id),
    [allQuestions, question]
  );
  const supersededBy = question?.superseded_by_question_id
    ? questionById.get(question.superseded_by_question_id)
    : null;
  const supersedes = question?.supersedes_question_id
    ? questionById.get(question.supersedes_question_id)
    : null;

  useEffect(() => {
    if (!question) {
      return;
    }
    setForm({
      childQuestionIds: [],
      hypothesis: question.hypothesis || "",
      noteIds: [],
      parentIds: getParentQuestionIds(question),
      reason: "",
      status: "staged",
      text: question.text || "",
      type: question.question_type || "descriptive",
    });
    setRefactorOpen(false);
  }, [question]);

  function updateForm(field, value) {
    setForm((current) => ({ ...current, [field]: value }));
  }

  function toggleListValue(field, value, checked) {
    setForm((current) => {
      const values = new Set(current[field]);
      if (checked) {
        values.add(value);
      } else {
        values.delete(value);
      }
      return { ...current, [field]: Array.from(values) };
    });
  }

  async function submitRefactor(event) {
    event.preventDefault();
    if (!question || !canWrite) {
      return;
    }
    if (!form.text.trim()) {
      setFlash("", "Replacement question text is required.");
      return;
    }
    if (!form.reason.trim()) {
      setFlash("", "Refactor reason is required.");
      return;
    }

    setBusy(true);
    setFlash("", "");
    try {
      const result = await apiRequest(`/questions/${question.question_id}/refactor`, {
        body: {
          child_question_ids_to_reparent: form.childQuestionIds,
          note_ids_to_retarget: form.noteIds,
          reason: form.reason.trim(),
          replacement: {
            hypothesis: form.hypothesis.trim() || null,
            parent_question_ids: form.parentIds,
            question_type: form.type,
            status: form.status,
            text: form.text.trim(),
          },
        },
        method: "POST",
        token,
      });
      setFlash("Question refactored.");
      const replacementId = result?.replacement_question?.question_id;
      if (replacementId) {
        navigate(`/app/questions/${replacementId}`);
      }
    } catch (err) {
      setFlash("", err.message || "Failed to refactor question.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <article className="card span-8">
      <div className="item-head">
        <h2>Question Detail</h2>
        {loading ? <span className="pill">Loading...</span> : null}
      </div>
      {error ? <p className="flash error">{error}</p> : null}
      {question ? (
        <div className="stack">
          {question.status === "superseded" ? (
            <div className="warn">
              Superseded
              {supersededBy ? (
                <>
                  {" "}
                  by{" "}
                  <button
                    type="button"
                    className="link"
                    onClick={() => navigate(`/app/questions/${supersededBy.question_id}`)}
                  >
                    {supersededBy.text}
                  </button>
                </>
              ) : question.superseded_by_question_id ? (
                <> by {question.superseded_by_question_id}</>
              ) : null}
            </div>
          ) : null}
          {question.supersedes_question_id ? (
            <div className="flash ok">
              Refines{" "}
              {supersedes ? (
                <button
                  type="button"
                  className="link"
                  onClick={() => navigate(`/app/questions/${supersedes.question_id}`)}
                >
                  {supersedes.text}
                </button>
              ) : (
                question.supersedes_question_id
              )}
            </div>
          ) : null}
          <div className="inline">
            <span className="pill">{question.status}</span>
            <span className="pill">{question.question_type}</span>
            {project ? <span className="pill">{project.name}</span> : null}
          </div>
          <p>{question.text}</p>
          {question.hypothesis ? <p className="subtle">Hypothesis: {question.hypothesis}</p> : null}
          {parentQuestionNames.length > 0 ? (
            <p className="subtle">Parents: {parentQuestionNames.join(" / ")}</p>
          ) : null}
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

          {Array.isArray(refactors) && refactors.length > 0 ? (
            <details className="context-details">
              <summary>Refactor history</summary>
              <div className="stack">
                {refactors.map((refactor) => (
                  <article key={refactor.refactor_id} className="item">
                    <div className="item-head">
                      <strong>{formatDate(refactor.created_at)}</strong>
                      <span className="pill">question refactor</span>
                    </div>
                    <p>{refactor.reason}</p>
                    <p className="mono">
                      {refactor.source_question_id} → {refactor.replacement_question_id}
                    </p>
                  </article>
                ))}
              </div>
            </details>
          ) : null}

          {canWrite && question.status !== "superseded" ? (
            <div className="stack refactor-panel">
              <button
                type="button"
                className="btn-secondary"
                onClick={() => setRefactorOpen((current) => !current)}
              >
                Refactor question
              </button>
              {refactorOpen ? (
                <form className="form" onSubmit={submitRefactor}>
                  <label>
                    Replacement question text
                    <textarea
                      value={form.text}
                      onChange={(event) => updateForm("text", event.target.value)}
                    />
                  </label>
                  <label>
                    Replacement type
                    <select
                      value={form.type}
                      onChange={(event) => updateForm("type", event.target.value)}
                    >
                      {QUESTION_TYPES.map((typeValue) => (
                        <option value={typeValue} key={typeValue}>
                          {typeValue}
                        </option>
                      ))}
                    </select>
                  </label>
                  <label>
                    Replacement status
                    <select
                      value={form.status}
                      onChange={(event) => updateForm("status", event.target.value)}
                    >
                      <option value="staged">staged</option>
                      <option value="active">active</option>
                    </select>
                  </label>
                  <label>
                    Replacement hypothesis
                    <input
                      value={form.hypothesis}
                      onChange={(event) => updateForm("hypothesis", event.target.value)}
                    />
                  </label>
                  <label>
                    Replacement parents
                    <select
                      multiple
                      value={form.parentIds}
                      onChange={(event) =>
                        updateForm(
                          "parentIds",
                          Array.from(event.target.selectedOptions || []).map(
                            (option) => option.value
                          )
                        )
                      }
                    >
                      {parentOptions.map((parent) => (
                        <option value={parent.question_id} key={parent.question_id}>
                          {parent.text}
                        </option>
                      ))}
                    </select>
                  </label>
                  <label>
                    Refactor reason
                    <textarea
                      value={form.reason}
                      onChange={(event) => updateForm("reason", event.target.value)}
                    />
                  </label>
                  {directChildren.length > 0 ? (
                    <fieldset>
                      <legend>Move child questions</legend>
                      {directChildren.map((child) => (
                        <label className="checkbox-row" key={child.question_id}>
                          <input
                            type="checkbox"
                            checked={form.childQuestionIds.includes(child.question_id)}
                            onChange={(event) =>
                              toggleListValue(
                                "childQuestionIds",
                                child.question_id,
                                event.target.checked
                              )
                            }
                          />
                          {child.text}
                        </label>
                      ))}
                    </fieldset>
                  ) : null}
                  {Array.isArray(targetedNotes) && targetedNotes.length > 0 ? (
                    <fieldset>
                      <legend>Move note targets</legend>
                      {targetedNotes.map((note) => (
                        <label className="checkbox-row" key={note.note_id}>
                          <input
                            type="checkbox"
                            checked={form.noteIds.includes(note.note_id)}
                            onChange={(event) =>
                              toggleListValue("noteIds", note.note_id, event.target.checked)
                            }
                          />
                          {compactNoteLabel(note)}
                        </label>
                      ))}
                    </fieldset>
                  ) : null}
                  <button type="submit" className="btn-primary">
                    Create replacement
                  </button>
                </form>
              ) : null}
            </div>
          ) : null}
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
