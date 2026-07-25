import * as React from "react";

import { useApiResource } from "../../hooks/useApiResource.js";
import { useProjectAccess } from "../../hooks/useProjectAccess.js";
import { apiListRequest, apiRequest, buildApiPath } from "../../shared/api.js";
import { formatDate } from "../../shared/formatters.js";
import { AppLink } from "../../shared/routing.jsx";

const { useCallback, useEffect, useMemo, useState } = React;
const MEMBER_PAGE_SIZE = 25;

function MemberList({
  kind,
  items,
  loading,
  meta,
  canRemove,
  navigate,
  onLoadPage,
  onRemove,
}) {
  const offset = Number(meta?.offset || 0);
  const total = Number(meta?.total ?? items.length);
  return (
    <div className="stack">
      <div className="item-head">
        <h3>{kind === "session" ? "Sessions" : "Datasets"}</h3>
        <span className="pill">{total}</span>
      </div>
      {loading ? <p className="subtle">Loading {kind}s...</p> : null}
      {!loading && items.length === 0 ? <p className="subtle">(none)</p> : null}
      {items.map((item) => {
        const id = item[`${kind}_id`];
        const label =
          kind === "session"
            ? `${item.session_type || "Session"} · ${formatDate(item.started_at)}`
            : `Dataset ${item.commit_hash || id}`;
        return (
          <div className="item" key={id}>
            <div className="item-head">
              <AppLink to={`/app/${kind}s/${id}`} navigate={navigate} className="link">
                <strong>{label}</strong>
              </AppLink>
              <span className="pill">{item.status}</span>
            </div>
            <p className="mono">{id}</p>
            {canRemove ? (
              <button
                type="button"
                className="btn-danger"
                onClick={() => onRemove(id)}
              >
                Remove from Experiment
              </button>
            ) : null}
          </div>
        );
      })}
      {total > MEMBER_PAGE_SIZE ? (
        <div className="inline">
          <button
            type="button"
            className="btn-secondary"
            disabled={loading || offset <= 0}
            onClick={() => onLoadPage(Math.max(0, offset - MEMBER_PAGE_SIZE))}
          >
            Previous {kind}s
          </button>
          <button
            type="button"
            className="btn-secondary"
            disabled={loading || offset + items.length >= total}
            onClick={() => onLoadPage(offset + MEMBER_PAGE_SIZE)}
          >
            Next {kind}s
          </button>
        </div>
      ) : null}
    </div>
  );
}

function ExperimentDetailCard({
  token,
  experimentId,
  projects,
  navigate,
  onSetActiveProject,
  canWrite: dashboardCanWrite,
  user = null,
}) {
  const {
    data: experiment,
    error: loadError,
    loading,
    setData: setExperiment,
  } = useApiResource(
    experimentId ? `/experiments/${experimentId}` : "",
    token,
    "Failed to load Experiment."
  );
  const { data: primaryQuestion } = useApiResource(
    experiment?.primary_question_id
      ? `/questions/${experiment.primary_question_id}`
      : "",
    token,
    "Failed to load the Experiment primary question."
  );
  const [actionError, setActionError] = useState("");
  const [actionBusy, setActionBusy] = useState(false);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [manageExpanded, setManageExpanded] = useState(false);
  const [candidateState, setCandidateState] = useState({
    datasets: [],
    error: "",
    loaded: false,
    loading: false,
    sessions: [],
  });
  const [selectedSessionId, setSelectedSessionId] = useState("");
  const [selectedDatasetId, setSelectedDatasetId] = useState("");
  const [sessionState, setSessionState] = useState({
    items: [],
    loading: false,
    meta: { limit: MEMBER_PAGE_SIZE, offset: 0, total: 0 },
  });
  const [datasetState, setDatasetState] = useState({
    items: [],
    loading: false,
    meta: { limit: MEMBER_PAGE_SIZE, offset: 0, total: 0 },
  });

  const project = useMemo(
    () =>
      experiment
        ? projects.find((item) => item.project_id === experiment.project_id) || null
        : null,
    [experiment, projects]
  );
  const experimentAccess = useProjectAccess(experiment?.project_id, {
    token,
    user,
    enabled: Boolean(experiment?.project_id),
  });
  const canWrite = experiment?.project_id
    ? experimentAccess.canContribute
    : dashboardCanWrite;

  useEffect(() => {
    setName(experiment?.name || "");
    setDescription(experiment?.description || "");
  }, [experiment]);

  useEffect(() => {
    setActionError("");
    setManageExpanded(false);
    setCandidateState({
      datasets: [],
      error: "",
      loaded: false,
      loading: false,
      sessions: [],
    });
    setSelectedSessionId("");
    setSelectedDatasetId("");
  }, [experimentId]);

  const loadMembers = useCallback(
    async (kind, offset = 0) => {
      if (!experimentId) {
        return;
      }
      const setter = kind === "session" ? setSessionState : setDatasetState;
      setter((current) => ({ ...current, loading: true }));
      try {
        const page = await apiListRequest(
          buildApiPath(`/experiments/${experimentId}/${kind}s`, {
            limit: MEMBER_PAGE_SIZE,
            offset,
          }),
          { token }
        );
        setter({ items: page.data, loading: false, meta: page.meta });
      } catch (err) {
        setActionError(err.message || `Failed to load Experiment ${kind}s.`);
        setter({
          items: [],
          loading: false,
          meta: { limit: MEMBER_PAGE_SIZE, offset: 0, total: 0 },
        });
      }
    },
    [experimentId, token]
  );

  useEffect(() => {
    if (!experimentId) {
      return;
    }
    loadMembers("session", 0);
    loadMembers("dataset", 0);
  }, [experimentId, loadMembers]);

  async function handleSave(event) {
    event.preventDefault();
    if (!canWrite || !experiment || experiment.status === "archived" || !name.trim()) {
      return;
    }
    setActionBusy(true);
    setActionError("");
    try {
      const updated = await apiRequest(`/experiments/${experimentId}`, {
        method: "PATCH",
        token,
        body: { name: name.trim(), description: description.trim() || null },
      });
      setExperiment(updated);
    } catch (err) {
      setActionError(err.message || "Failed to update Experiment.");
    } finally {
      setActionBusy(false);
    }
  }

  async function advanceStatus(status) {
    if (!canWrite || !experiment) {
      return;
    }
    setActionBusy(true);
    setActionError("");
    try {
      const updated = await apiRequest(`/experiments/${experimentId}`, {
        method: "PATCH",
        token,
        body: { status },
      });
      setExperiment(updated);
    } catch (err) {
      setActionError(err.message || "Failed to update Experiment lifecycle.");
    } finally {
      setActionBusy(false);
    }
  }

  async function toggleManage() {
    const nextExpanded = !manageExpanded;
    setManageExpanded(nextExpanded);
    if (!nextExpanded || candidateState.loading || candidateState.loaded) {
      return;
    }
    setCandidateState((current) => ({ ...current, error: "", loading: true }));
    try {
      const [sessionPage, datasetPage] = await Promise.all([
        apiListRequest(
          buildApiPath("/sessions", {
            project_id: experiment.project_id,
            limit: 200,
            offset: 0,
          }),
          { token }
        ),
        apiListRequest(
          buildApiPath("/datasets/summaries", {
            project_id: experiment.project_id,
            limit: 200,
            offset: 0,
          }),
          { token }
        ),
      ]);
      setCandidateState({
        datasets: datasetPage.data,
        error: "",
        loaded: true,
        loading: false,
        sessions: sessionPage.data,
      });
    } catch (err) {
      setCandidateState({
        datasets: [],
        error: err.message || "Failed to load membership candidates.",
        loaded: true,
        loading: false,
        sessions: [],
      });
    }
  }

  async function updateMembership(kind, entityId, method) {
    if (!canWrite || !entityId) {
      return;
    }
    setActionBusy(true);
    setActionError("");
    try {
      await apiRequest(`/experiments/${experimentId}/${kind}s/${entityId}`, {
        method,
        token,
      });
      await loadMembers(kind, 0);
      if (kind === "session") {
        setSelectedSessionId("");
      } else {
        setSelectedDatasetId("");
      }
    } catch (err) {
      setActionError(err.message || `Failed to update Experiment ${kind} membership.`);
    } finally {
      setActionBusy(false);
    }
  }

  const archived = experiment?.status === "archived";
  const canAddSessions = canWrite && experiment?.status === "active";
  const canAddDatasets =
    canWrite && (experiment?.status === "active" || experiment?.status === "closed");
  const error = actionError || loadError;

  return (
    <article className="card span-12">
      <div className="item-head">
        <h2>Experiment Detail</h2>
        {loading ? <span className="pill">Loading...</span> : null}
      </div>
      {error ? <p className="flash error">{error}</p> : null}

      {experiment ? (
        <div className="stack">
          <div className="inline">
            <span className="pill">{experiment.status}</span>
            {project ? <span className="pill">{project.name}</span> : null}
            <span className="subtle">Created {formatDate(experiment.created_at)}</span>
          </div>
          <p className="mono">{experiment.experiment_id}</p>
          <div className="stack">
            <div className="subtle">Primary question</div>
            <AppLink
              to={`/app/questions/${experiment.primary_question_id}`}
              navigate={navigate}
              className="link"
            >
              <strong>{primaryQuestion?.text || experiment.primary_question_id}</strong>
            </AppLink>
            <span className="mono">{experiment.primary_question_id}</span>
          </div>

          <form className="form" onSubmit={handleSave}>
            <label>
              Experiment name
              <input
                value={name}
                onChange={(event) => setName(event.target.value)}
                disabled={!canWrite || archived || actionBusy}
              />
            </label>
            <label>
              Description
              <textarea
                value={description}
                onChange={(event) => setDescription(event.target.value)}
                disabled={!canWrite || archived || actionBusy}
              />
            </label>
            <button
              type="submit"
              className="btn-secondary"
              disabled={!canWrite || archived || actionBusy || !name.trim()}
            >
              Save Experiment
            </button>
          </form>

          <div className="inline">
            {experiment.status === "active" ? (
              <button
                type="button"
                className="btn-danger"
                disabled={!canWrite || actionBusy}
                onClick={() => advanceStatus("closed")}
              >
                Close Experiment
              </button>
            ) : null}
            {experiment.status === "closed" ? (
              <button
                type="button"
                className="btn-danger"
                disabled={!canWrite || actionBusy}
                onClick={() => advanceStatus("archived")}
              >
                Archive Experiment
              </button>
            ) : null}
          </div>

          <MemberList
            kind="session"
            items={sessionState.items}
            loading={sessionState.loading}
            meta={sessionState.meta}
            canRemove={canWrite && !archived}
            navigate={navigate}
            onLoadPage={(offset) => loadMembers("session", offset)}
            onRemove={(id) => updateMembership("session", id, "DELETE")}
          />
          <MemberList
            kind="dataset"
            items={datasetState.items}
            loading={datasetState.loading}
            meta={datasetState.meta}
            canRemove={canWrite && !archived}
            navigate={navigate}
            onLoadPage={(offset) => loadMembers("dataset", offset)}
            onRemove={(id) => updateMembership("dataset", id, "DELETE")}
          />

          {!archived ? (
            <div className="stack">
              <button
                type="button"
                className="btn-secondary"
                aria-expanded={manageExpanded}
                onClick={toggleManage}
              >
                {manageExpanded ? "Hide membership controls" : "Manage memberships"}
              </button>
              {manageExpanded ? (
                <div className="stack">
                  {candidateState.loading ? (
                    <p className="subtle">Loading project Sessions and Datasets...</p>
                  ) : null}
                  {candidateState.error ? (
                    <p className="flash error">{candidateState.error}</p>
                  ) : null}
                  <label>
                    Add Session
                    <select
                      value={selectedSessionId}
                      disabled={!canAddSessions || candidateState.loading}
                      onChange={(event) => setSelectedSessionId(event.target.value)}
                    >
                      <option value="">Select a Session</option>
                      {candidateState.sessions.map((session) => (
                        <option value={session.session_id} key={session.session_id}>
                          {session.session_type} · {formatDate(session.started_at)}
                        </option>
                      ))}
                    </select>
                  </label>
                  <button
                    type="button"
                    className="btn-secondary"
                    disabled={!canAddSessions || !selectedSessionId || actionBusy}
                    onClick={() =>
                      updateMembership("session", selectedSessionId, "PUT")
                    }
                  >
                    Add Session
                  </button>
                  {experiment.status === "closed" ? (
                    <p className="subtle">
                      Closed Experiments accept Dataset results but no new Sessions.
                    </p>
                  ) : null}
                  <label>
                    Add Dataset
                    <select
                      value={selectedDatasetId}
                      disabled={!canAddDatasets || candidateState.loading}
                      onChange={(event) => setSelectedDatasetId(event.target.value)}
                    >
                      <option value="">Select a Dataset</option>
                      {candidateState.datasets.map((dataset) => (
                        <option value={dataset.dataset_id} key={dataset.dataset_id}>
                          {dataset.commit_hash || dataset.dataset_id}
                        </option>
                      ))}
                    </select>
                  </label>
                  <button
                    type="button"
                    className="btn-secondary"
                    disabled={!canAddDatasets || !selectedDatasetId || actionBusy}
                    onClick={() =>
                      updateMembership("dataset", selectedDatasetId, "PUT")
                    }
                  >
                    Add Dataset
                  </button>
                </div>
              ) : null}
            </div>
          ) : null}
        </div>
      ) : null}

      <div className="inline detail-actions">
        <button type="button" className="btn-secondary" onClick={() => navigate("/app")}>
          Back
        </button>
        {experiment ? (
          <button
            type="button"
            className="btn-primary"
            onClick={() => {
              onSetActiveProject(experiment.project_id);
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

export { ExperimentDetailCard };
