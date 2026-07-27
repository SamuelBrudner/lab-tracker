import * as React from "react";

import { apiListRequest, apiRequest, buildApiPath } from "../../shared/api.js";
import { formatDate } from "../../shared/formatters.js";
import { AppLink } from "../../shared/routing.jsx";
import { DraftRecoveryNotice } from "../../shared/ui.jsx";
import { useLocalDraft } from "../../hooks/useLocalDraft.js";

const { useCallback, useEffect, useState } = React;
const EXPERIMENT_PAGE_SIZE = 20;

function ExperimentPanel({
  token,
  canWrite,
  selectedProjectId,
  questions,
  navigate,
}) {
  const [state, setState] = useState({
    error: "",
    items: [],
    loading: false,
    meta: { limit: EXPERIMENT_PAGE_SIZE, offset: 0, total: 0 },
  });
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const descriptionDraft = useLocalDraft({
    baseline: "",
    key: selectedProjectId ? `experiment-description:${selectedProjectId}` : "",
    value: description,
  });
  const [primaryQuestionId, setPrimaryQuestionId] = useState("");
  const [createBusy, setCreateBusy] = useState(false);

  const loadPage = useCallback(
    async (offset = 0) => {
      if (!selectedProjectId) {
        setState({
          error: "",
          items: [],
          loading: false,
          meta: { limit: EXPERIMENT_PAGE_SIZE, offset: 0, total: 0 },
        });
        return;
      }
      setState((current) => ({ ...current, error: "", loading: true }));
      try {
        const page = await apiListRequest(
          buildApiPath("/experiments", {
            project_id: selectedProjectId,
            limit: EXPERIMENT_PAGE_SIZE,
            offset,
          }),
          { token }
        );
        setState({ error: "", items: page.data, loading: false, meta: page.meta });
      } catch (err) {
        setState({
          error: err.message || "Failed to load Experiments.",
          items: [],
          loading: false,
          meta: { limit: EXPERIMENT_PAGE_SIZE, offset: 0, total: 0 },
        });
      }
    },
    [selectedProjectId, token]
  );

  useEffect(() => {
    loadPage(0);
  }, [loadPage]);

  useEffect(() => {
    const activeQuestions = (questions || []).filter((question) => question.status === "active");
    setPrimaryQuestionId((current) =>
      activeQuestions.some((question) => question.question_id === current)
        ? current
        : activeQuestions[0]?.question_id || ""
    );
  }, [questions]);

  async function handleCreate(event) {
    event.preventDefault();
    if (!canWrite || !selectedProjectId || !name.trim() || !primaryQuestionId) {
      return;
    }
    setCreateBusy(true);
    setState((current) => ({ ...current, error: "" }));
    try {
      const experiment = await apiRequest("/experiments", {
        method: "POST",
        token,
        body: {
          project_id: selectedProjectId,
          name: name.trim(),
          description: description.trim() || null,
          primary_question_id: primaryQuestionId,
        },
      });
      setName("");
      setDescription("");
      await loadPage(0);
      if (experiment?.experiment_id) {
        navigate(`/app/experiments/${experiment.experiment_id}`);
      }
    } catch (err) {
      setState((current) => ({
        ...current,
        error: err.message || "Failed to create Experiment.",
      }));
    } finally {
      setCreateBusy(false);
    }
  }

  const activeQuestions = (questions || []).filter(
    (question) => question.status === "active"
  );
  const offset = Number(state.meta?.offset || 0);
  const total = Number(state.meta?.total ?? state.items.length);

  return (
    <article className="card span-12">
      <div className="item-head">
        <h2>Experiments</h2>
        <span className="pill">{total}</span>
      </div>
      <p className="subtle">
        Group the Sessions and Datasets that belong to one scientific run without turning every
        trial file into a work-graph node.
      </p>

      <form className="form" onSubmit={handleCreate}>
        <label>
          Experiment name
          <input
            value={name}
            onChange={(event) => setName(event.target.value)}
            disabled={!canWrite || !selectedProjectId}
          />
        </label>
        <DraftRecoveryNotice
          label="an unsaved experiment description"
          savedAt={descriptionDraft.recoveredAt}
          onRestore={() => {
            const restored = descriptionDraft.restore();
            if (restored !== null) {
              setDescription(restored);
            }
          }}
          onDiscard={descriptionDraft.discard}
        />
        <label>
          Description (optional)
          <textarea
            value={description}
            onChange={(event) => setDescription(event.target.value)}
            disabled={!canWrite || !selectedProjectId}
          />
        </label>
        <label>
          Primary question
          <select
            value={primaryQuestionId}
            onChange={(event) => setPrimaryQuestionId(event.target.value)}
            disabled={!canWrite || !selectedProjectId || activeQuestions.length === 0}
          >
            <option value="">Select an active question</option>
            {activeQuestions.map((question) => (
              <option value={question.question_id} key={question.question_id}>
                {question.text}
              </option>
            ))}
          </select>
        </label>
        <button
          type="submit"
          className="btn-primary"
          disabled={
            !canWrite ||
            !selectedProjectId ||
            !name.trim() ||
            !primaryQuestionId ||
            createBusy
          }
        >
          {createBusy ? "Creating..." : "Create Experiment"}
        </button>
      </form>

      {state.loading ? <p className="subtle">Loading Experiments...</p> : null}
      {state.error ? <p className="flash error">{state.error}</p> : null}
      {!state.loading && !state.error && state.items.length === 0 ? (
        <p className="subtle">No Experiments group this project yet.</p>
      ) : null}

      <div className="stack">
        {state.items.map((experiment) => (
          <article className="item" key={experiment.experiment_id}>
            <div className="item-head">
              <AppLink
                to={`/app/experiments/${experiment.experiment_id}`}
                navigate={navigate}
                className="link"
              >
                <strong>{experiment.name}</strong>
              </AppLink>
              <span className="pill">{experiment.status}</span>
            </div>
            {experiment.description ? <p>{experiment.description}</p> : null}
            <p className="subtle">Updated {formatDate(experiment.updated_at)}</p>
          </article>
        ))}
      </div>

      {total > EXPERIMENT_PAGE_SIZE ? (
        <div className="inline">
          <button
            type="button"
            className="btn-secondary"
            disabled={state.loading || offset <= 0}
            onClick={() => loadPage(Math.max(0, offset - EXPERIMENT_PAGE_SIZE))}
          >
            Previous Experiments
          </button>
          <span className="subtle">
            {offset + 1}-{Math.min(offset + state.items.length, total)} of {total}
          </span>
          <button
            type="button"
            className="btn-secondary"
            disabled={state.loading || offset + state.items.length >= total}
            onClick={() => loadPage(offset + EXPERIMENT_PAGE_SIZE)}
          >
            Next Experiments
          </button>
        </div>
      ) : null}
    </article>
  );
}

export { ExperimentPanel };
