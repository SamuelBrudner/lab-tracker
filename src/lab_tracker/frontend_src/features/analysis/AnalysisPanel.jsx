import * as React from "react";

import { formatDate } from "../../shared/formatters.js";
import { AppLink } from "../../shared/routing.jsx";

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

export { AnalysisPanel };
