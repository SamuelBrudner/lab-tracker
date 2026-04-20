import * as React from "react";

import { apiRequest } from "../shared/api.js";
import { formatDate } from "../shared/formatters.js";
import { AppLink } from "../shared/routing.jsx";
import { useApiResource } from "../hooks/useApiResource.js";

const { useMemo, useState } = React;

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
      statusFilter === "all" ? items : items.filter((analysis) => analysis.status === statusFilter);
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
              dataset: datasetsById[datasetId] || null,
              datasetId,
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
                  <p className="warn">Commit requires all linked datasets to be committed.</p>
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
        label: project.name,
        value: project.project_id,
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
      {showEmptyState ? <p className="subtle">No hits for &quot;{lastRanQuery}&quot;.</p> : null}

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

function VisualizationDetailCard({ token, vizId, navigate }) {
  const { data: viz, error, loading } = useApiResource(
    token && vizId ? `/visualizations/${vizId}` : "",
    token,
    "Failed to load visualization."
  );

  return (
    <article className="card span-8">
      <div className="item-head">
        <h2>Visualization Detail</h2>
        {loading ? <span className="pill">Loading...</span> : null}
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

export { AnalysisPanel, SearchPanel, VisualizationDetailCard };
