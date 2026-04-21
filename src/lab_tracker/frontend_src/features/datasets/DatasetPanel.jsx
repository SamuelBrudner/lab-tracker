import * as React from "react";

import { formatBytes, formatDate } from "../../shared/formatters.js";

const { useEffect } = React;

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
  reviewPolicy,
  datasetReviewsById,
}) {
  useEffect(() => {
    if (!selectedProjectId) {
      return;
    }

    datasets
      .filter((dataset) => dataset.status !== "committed")
      .forEach((dataset) => {
        const current = datasetFilesById[dataset.dataset_id];
        if (current) {
          return;
        }
        onLoadDatasetFiles(dataset.dataset_id);
      });
  }, [datasets, datasetFilesById, onLoadDatasetFiles, selectedProjectId]);

  const reviewRequired = Boolean(reviewPolicy && reviewPolicy !== "none");

  return (
    <article className="card span-6">
      <div className="item-head">
        <h2>Dataset Review</h2>
        {reviewPolicy ? <span className="pill">review: {reviewPolicy}</span> : null}
      </div>
      <p className="subtle">
        Stage datasets against active questions, then{" "}
        {reviewRequired ? "submit for PI approval." : "commit when ready."}
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

      <div className="stack">
        {datasets.map((dataset) => {
          const reviewState = datasetReviewsById ? datasetReviewsById[dataset.dataset_id] : null;
          const review = reviewState?.review || null;
          const reviewStatus = review?.status ? String(review.status) : "";
          const reviewStatusLabel = reviewStatus.replace(/_/g, " ");
          const reviewStatusClass = reviewStatus ? `pill review-status review-${reviewStatus}` : "";
          const reviewPending = reviewStatus === "pending";
          const reviewResolved = Boolean(reviewStatus && !reviewPending);
          const reviewLocked = reviewRequired && reviewPending;

          const commitLabel = !reviewRequired
            ? "Commit dataset"
            : reviewPending
              ? "Awaiting PI review"
              : reviewResolved
                ? "Resubmit for review"
                : "Submit for PI review";

          return (
            <article className="item" key={dataset.dataset_id}>
              <div className="item-head">
                <strong>{dataset.status}</strong>
                <div className="inline">
                  {reviewRequired && reviewStatus ? (
                    <span className={reviewStatusClass}>{reviewStatusLabel}</span>
                  ) : null}
                  <span className="subtle">{formatDate(dataset.created_at)}</span>
                </div>
              </div>
              <p className="mono">{dataset.dataset_id}</p>
              <p className="mono">commit hash: {dataset.commit_hash}</p>
              <p>
                Links:{" "}
                {dataset.question_links.map((link) => `${link.role}:${link.question_id}`).join(" | ")}
              </p>

              {reviewRequired ? (
                <div className="stack">
                  <div className="subtle">Review</div>
                  {reviewState?.loading ? <p className="subtle">Loading review status...</p> : null}
                  {reviewState?.error ? (
                    <p className="subtle">Review unavailable: {reviewState.error}</p>
                  ) : null}
                  {review ? (
                    <>
                      <div className="inline">
                        {reviewStatus ? (
                          <span className={reviewStatusClass}>{reviewStatusLabel}</span>
                        ) : null}
                        {review.reviewer_user_id ? (
                          <span className="pill mono" title="Reviewer user_id">
                            {review.reviewer_user_id}
                          </span>
                        ) : (
                          <span className="pill">unassigned</span>
                        )}
                        <span className="subtle">
                          {formatDate(review.resolved_at || review.requested_at)}
                        </span>
                      </div>
                      {review.comments ? <p className="source-snippet">{review.comments}</p> : null}
                    </>
                  ) : null}
                  {reviewLocked ? (
                    <p className="warn">Review requested. Attachments are locked until resolved.</p>
                  ) : null}
                </div>
              ) : null}

              <div className="stack">
                <div className="item">
                  <div className="item-head">
                    <strong>Files</strong>
                    <span className="subtle">
                      {dataset.status === "committed"
                        ? `${(dataset.commit_manifest?.files || []).length} committed`
                        : (() => {
                            const state = datasetFilesById[dataset.dataset_id];
                            if (!state || state.loading) {
                              return "loading...";
                            }
                            if (state.error) {
                              return "unavailable";
                            }
                            return `${state.items.length} attached`;
                          })()}
                    </span>
                  </div>

                  {dataset.status === "staged" ? (
                    <label>
                      Attach file(s)
                      <input
                        type="file"
                        multiple
                        disabled={!canWrite || busy || reviewLocked}
                        onChange={(event) => {
                          const files = Array.from(event.target.files || []);
                          event.target.value = "";
                          onUploadDatasetFiles(dataset.dataset_id, files);
                        }}
                      />
                    </label>
                  ) : null}

                  {dataset.status === "committed" ? null : (() => {
                    const state = datasetFilesById[dataset.dataset_id];
                    if (!state) {
                      return null;
                    }
                    if (state.loading) {
                      return <p className="subtle">Loading attached files...</p>;
                    }
                    if (state.error) {
                      return (
                        <div className="stack">
                          <p className="subtle">Unable to load attached files: {state.error}</p>
                          <div className="inline">
                            <button
                              type="button"
                              className="btn-secondary"
                              disabled={busy}
                              onClick={() => onLoadDatasetFiles(dataset.dataset_id)}
                            >
                              Retry
                            </button>
                          </div>
                        </div>
                      );
                    }
                    if (state.items.length === 0) {
                      return (
                        <p className="warn">
                          Attach at least one file before committing this dataset.
                        </p>
                      );
                    }
                    return null;
                  })()}

                  <div className="stack">
                    {(dataset.status === "committed"
                      ? dataset.commit_manifest?.files || []
                      : datasetFilesById[dataset.dataset_id]?.items || []
                    ).map((file) => (
                      <div className="item" key={file.file_id || file.path}>
                        <div className="item-head">
                          <span className="mono">{file.path}</span>
                          <span className="subtle">{formatBytes(file.size_bytes)}</span>
                        </div>
                        <p className="mono">sha256: {file.checksum}</p>
                        {dataset.status === "staged" && file.file_id ? (
                          <button
                            type="button"
                            className="btn-danger"
                            disabled={!canWrite || busy || reviewLocked}
                            onClick={() => onDeleteDatasetFile(dataset.dataset_id, file.file_id)}
                          >
                            Remove file
                          </button>
                        ) : null}
                      </div>
                    ))}
                  </div>
                </div>
              </div>

              {dataset.status !== "committed" ? (
                <button
                  type="button"
                  className="btn-primary"
                  disabled={(() => {
                    if (!canWrite || busy || reviewLocked) {
                      return true;
                    }
                    const state = datasetFilesById[dataset.dataset_id];
                    if (!state || state.loading) {
                      return true;
                    }
                    if (state.error) {
                      return false;
                    }
                    return state.items.length === 0;
                  })()}
                  onClick={() => onCommitDataset(dataset.dataset_id)}
                >
                  {commitLabel}
                </button>
              ) : null}
            </article>
          );
        })}
      </div>
    </article>
  );
}

export { DatasetPanel };
