import * as React from "react";

import { formatBytes, formatDate } from "../../shared/formatters.js";

const { useState } = React;

function StagedDatasetItem({
  busy,
  canWrite,
  dataset,
  fileState,
  onCommitDataset,
  onDeleteDatasetFile,
  onLoadDatasetFiles,
  onUploadDatasetFiles,
}) {
  const [expanded, setExpanded] = useState(false);

  const handleToggleFiles = async () => {
    const nextExpanded = !expanded;
    setExpanded(nextExpanded);
    if (nextExpanded && (!fileState || (!fileState.loaded && !fileState.loading))) {
      await onLoadDatasetFiles(dataset.dataset_id);
    }
  };

  const attachedFiles = fileState?.items || [];
  const filesReady = fileState && fileState.loaded && !fileState.loading && !fileState.error;

  return (
    <article className="item">
      <div className="item-head">
        <strong>{dataset.status}</strong>
        <div className="inline">
          <span className="subtle">{formatDate(dataset.created_at)}</span>
          <button type="button" className="btn-secondary" onClick={handleToggleFiles}>
            {expanded ? "Hide files" : "Manage files"}
          </button>
        </div>
      </div>
      <p className="mono">{dataset.dataset_id}</p>
      <p className="mono">commit hash: {dataset.commit_hash}</p>
      <p>Links: {dataset.question_links.map((link) => `${link.role}:${link.question_id}`).join(" | ")}</p>

      {expanded ? (
        <div className="stack">
          <div className="item">
            <div className="item-head">
              <strong>Files</strong>
              <span className="subtle">
                {!fileState || fileState.loading
                  ? "loading..."
                  : fileState.error
                    ? "unavailable"
                    : `${attachedFiles.length} attached`}
              </span>
            </div>

            <label>
              Attach file(s)
              <input
                type="file"
                multiple
                disabled={!canWrite || busy}
                onChange={(event) => {
                  const files = Array.from(event.target.files || []);
                  event.target.value = "";
                  onUploadDatasetFiles(dataset.dataset_id, files);
                }}
              />
            </label>

            {!fileState || fileState.loading ? (
              <p className="subtle">Loading attached files...</p>
            ) : null}
            {fileState?.error ? (
              <div className="stack">
                <p className="subtle">Unable to load attached files: {fileState.error}</p>
                <div className="inline">
                  <button
                    type="button"
                    className="btn-secondary"
                    disabled={busy}
                    onClick={() => onLoadDatasetFiles(dataset.dataset_id, { force: true })}
                  >
                    Retry
                  </button>
                </div>
              </div>
            ) : null}
            {filesReady && attachedFiles.length === 0 ? (
              <p className="warn">Attach at least one file before committing this dataset.</p>
            ) : null}

            <div className="stack">
              {attachedFiles.map((file) => (
                <div className="item" key={file.file_id || file.path}>
                  <div className="item-head">
                    <span className="mono">{file.path}</span>
                    <span className="subtle">{formatBytes(file.size_bytes)}</span>
                  </div>
                  <p className="mono">sha256: {file.checksum}</p>
                  {file.file_id ? (
                    <button
                      type="button"
                      className="btn-danger"
                      disabled={!canWrite || busy}
                      onClick={() => onDeleteDatasetFile(dataset.dataset_id, file.file_id)}
                    >
                      Remove file
                    </button>
                  ) : null}
                </div>
              ))}
            </div>

            <button
              type="button"
              className="btn-primary"
              disabled={!canWrite || busy || !filesReady || attachedFiles.length === 0}
              onClick={() => onCommitDataset(dataset.dataset_id)}
            >
              Commit dataset
            </button>
          </div>
        </div>
      ) : null}
    </article>
  );
}

function DatasetPanel({
  canWrite,
  busy,
  selectedProjectId,
  datasetPrimaryQuestionId,
  onDatasetPrimaryQuestionIdChange,
  datasetSecondaryRaw,
  onDatasetSecondaryRawChange,
  onCreateDataset,
  questions,
  datasets,
  onCommitDataset,
  datasetFilesById,
  onLoadDatasetFiles,
  onUploadDatasetFiles,
  onDeleteDatasetFile,
}) {
  return (
    <article className="card span-6">
      <div className="item-head">
        <h2>Dataset Queue</h2>
      </div>
      <p className="subtle">
        Stage datasets against active questions, attach files when needed, and commit them from the
        active work queue.
      </p>

      <form className="form" onSubmit={onCreateDataset}>
        <label>
          Primary question
          <select
            value={datasetPrimaryQuestionId}
            onChange={onDatasetPrimaryQuestionIdChange}
            disabled={!canWrite || !selectedProjectId || questions.length === 0}
          >
            <option value="">Select question</option>
            {questions.map((question) => (
              <option value={question.question_id} key={question.question_id}>
                {question.text}
              </option>
            ))}
          </select>
        </label>
        <label>
          Secondary question IDs (comma-separated UUIDs)
          <input
            value={datasetSecondaryRaw}
            onChange={onDatasetSecondaryRawChange}
            disabled={!canWrite || !selectedProjectId}
          />
        </label>
        <button className="btn-secondary" disabled={!canWrite || !selectedProjectId || busy}>
          Stage dataset
        </button>
      </form>

      <div className="item-head">
        <h3>Staged Work</h3>
        <span className="pill">{datasets.length} staged</span>
      </div>

      {datasets.length === 0 ? (
        <p className="subtle">No staged datasets for this project.</p>
      ) : (
        <div className="stack">
          {datasets.map((dataset) => (
            <StagedDatasetItem
              key={dataset.dataset_id}
              busy={busy}
              canWrite={canWrite}
              dataset={dataset}
              fileState={datasetFilesById[dataset.dataset_id]}
              onCommitDataset={onCommitDataset}
              onDeleteDatasetFile={onDeleteDatasetFile}
              onLoadDatasetFiles={onLoadDatasetFiles}
              onUploadDatasetFiles={onUploadDatasetFiles}
            />
          ))}
        </div>
      )}
    </article>
  );
}

export { DatasetPanel };
