import * as React from "react";

import { formatDate } from "../../shared/formatters.js";
import { AppLink } from "../../shared/routing.jsx";

const { useMemo, useState } = React;

function AnalysisStageForm({
  analysisCodeVersion,
  analysisDatasetIds,
  analysisEnvironmentHash,
  analysisMethodHash,
  busy,
  canWrite,
  datasetOptions,
  onAnalysisCodeVersionChange,
  onAnalysisDatasetIdsChange,
  onAnalysisEnvironmentHashChange,
  onAnalysisMethodHashChange,
  onCreateAnalysis,
  selectedProjectId,
}) {
  return (
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
  );
}

function AnalysisVisualizationSection({ analysisId, onLoadVisualizations, state, navigate }) {
  const [expanded, setExpanded] = useState(false);

  const handleToggle = async () => {
    const nextExpanded = !expanded;
    setExpanded(nextExpanded);
    if (nextExpanded && (!state || (!state.loaded && !state.loading))) {
      await onLoadVisualizations(analysisId);
    }
  };

  return (
    <div className="stack">
      <div className="item-head">
        <h4>Visualizations</h4>
        <button type="button" className="btn-secondary" onClick={handleToggle}>
          {expanded ? "Hide visualizations" : "Load visualizations"}
        </button>
      </div>

      {expanded ? (
        <>
          {!state || state.loading ? <p className="subtle">Loading visualizations...</p> : null}
          {state?.error ? <p className="flash error">{state.error}</p> : null}
          {state && !state.loading && !state.error && state.items.length === 0 ? (
            <p className="subtle">No visualizations recorded for this analysis.</p>
          ) : null}
          <div className="stack">
            {(state?.items || []).map((viz) => (
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
                <p className="mono">{viz.viz_id}</p>
                <p className="mono">{viz.file_path}</p>
              </article>
            ))}
          </div>
        </>
      ) : null}
    </div>
  );
}

function AnalysisQueueItem({
  analysis,
  busy,
  canArchive,
  canCommit,
  datasetsById,
  navigate,
  onArchiveAnalysis,
  onCommitAnalysis,
  onLoadVisualizations,
  showVisualizations = false,
  visualizationState,
}) {
  const datasetsForAnalysis = (analysis.dataset_ids || []).map((datasetId) => ({
    dataset: datasetsById[datasetId] || null,
    datasetId,
  }));

  return (
    <article className="item">
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
      </div>

      {showVisualizations ? (
        <AnalysisVisualizationSection
          analysisId={analysis.analysis_id}
          navigate={navigate}
          onLoadVisualizations={onLoadVisualizations}
          state={visualizationState}
        />
      ) : null}

      <div className="inline">
        {canCommit ? (
          <button
            type="button"
            className="btn-primary"
            disabled={busy}
            onClick={() => onCommitAnalysis(analysis.analysis_id)}
          >
            Commit analysis
          </button>
        ) : null}
        {canArchive ? (
          <button
            type="button"
            className="btn-danger"
            disabled={busy}
            onClick={() => onArchiveAnalysis(analysis.analysis_id)}
          >
            Archive analysis
          </button>
        ) : null}
      </div>
    </article>
  );
}

function AnalysisPanel({
  canWrite,
  busy,
  loading,
  error,
  selectedProjectId,
  datasets,
  stagedAnalyses,
  recentCommittedAnalyses,
  visualizationStates,
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
  onLoadVisualizations,
  navigate,
}) {
  const datasetsById = useMemo(() => {
    const index = {};
    (datasets || []).forEach((dataset) => {
      index[dataset.dataset_id] = dataset;
    });
    return index;
  }, [datasets]);

  const datasetOptions = useMemo(() => {
    const items = Array.isArray(datasets) ? [...datasets] : [];
    items.sort((left, right) => {
      const leftTime = Date.parse(left.updated_at || left.created_at || "") || 0;
      const rightTime = Date.parse(right.updated_at || right.created_at || "") || 0;
      return rightTime - leftTime;
    });
    return items;
  }, [datasets]);

  return (
    <article className="card span-12">
      <div className="item-head">
        <h2>Analysis Queue</h2>
        <div className="inline">
          <span className="pill">{stagedAnalyses.length} staged</span>
          <span className="pill">{recentCommittedAnalyses.length} recent committed</span>
        </div>
      </div>
      <p className="subtle">
        Stage analysis work against datasets, then promote and review only the current active queue.
      </p>

      <AnalysisStageForm
        analysisCodeVersion={analysisCodeVersion}
        analysisDatasetIds={analysisDatasetIds}
        analysisEnvironmentHash={analysisEnvironmentHash}
        analysisMethodHash={analysisMethodHash}
        busy={busy}
        canWrite={canWrite}
        datasetOptions={datasetOptions}
        onAnalysisCodeVersionChange={onAnalysisCodeVersionChange}
        onAnalysisDatasetIdsChange={onAnalysisDatasetIdsChange}
        onAnalysisEnvironmentHashChange={onAnalysisEnvironmentHashChange}
        onAnalysisMethodHashChange={onAnalysisMethodHashChange}
        onCreateAnalysis={onCreateAnalysis}
        selectedProjectId={selectedProjectId}
      />

      {!canWrite ? (
        <p className="warn">Your role is read-only. Ask an admin/editor to register analyses.</p>
      ) : null}
      {loading ? <p className="subtle">Loading analysis work...</p> : null}
      {error ? <p className="flash error">{error}</p> : null}

      <div className="item-head" style={{ marginTop: "0.85rem" }}>
        <h3>Staged Analyses</h3>
      </div>
      {stagedAnalyses.length === 0 ? (
        <p className="subtle">No staged analyses for this project.</p>
      ) : (
        <div className="stack">
          {stagedAnalyses.map((analysis) => {
            const commitBlocked =
              (analysis.dataset_ids || []).length === 0 ||
              (analysis.dataset_ids || []).some((datasetId) => {
                const dataset = datasetsById[datasetId];
                return !dataset || dataset.status !== "committed";
              });

            return (
              <AnalysisQueueItem
                key={analysis.analysis_id}
                analysis={analysis}
                busy={busy}
                canArchive={false}
                canCommit={canWrite && !commitBlocked}
                datasetsById={datasetsById}
                navigate={navigate}
                onArchiveAnalysis={onArchiveAnalysis}
                onCommitAnalysis={onCommitAnalysis}
                onLoadVisualizations={onLoadVisualizations}
                visualizationState={visualizationStates[analysis.analysis_id]}
              />
            );
          })}
        </div>
      )}

      <div className="item-head" style={{ marginTop: "0.85rem" }}>
        <h3>Recent Committed</h3>
      </div>
      {recentCommittedAnalyses.length === 0 ? (
        <p className="subtle">No committed analyses in the recent queue.</p>
      ) : (
        <div className="stack">
          {recentCommittedAnalyses.map((analysis) => (
            <AnalysisQueueItem
              key={analysis.analysis_id}
              analysis={analysis}
              busy={busy}
              canArchive={canWrite}
              canCommit={false}
              datasetsById={datasetsById}
              navigate={navigate}
              onArchiveAnalysis={onArchiveAnalysis}
              onCommitAnalysis={onCommitAnalysis}
              onLoadVisualizations={onLoadVisualizations}
              showVisualizations={true}
              visualizationState={visualizationStates[analysis.analysis_id]}
            />
          ))}
        </div>
      )}
    </article>
  );
}

export { AnalysisPanel };
