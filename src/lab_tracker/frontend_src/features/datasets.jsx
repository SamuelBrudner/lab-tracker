import * as React from "react";

import { fetchAllPages } from "../shared/api.js";
import { formatBytes, formatDate } from "../shared/formatters.js";
import { AppLink } from "../shared/routing.jsx";
import { useApiResource } from "../hooks/useApiResource.js";

const { useEffect, useMemo, useState } = React;

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

function DatasetDetailCard({ token, datasetId, projects, navigate, onSetActiveProject }) {
  const { data: dataset, error, loading } = useApiResource(
    token && datasetId ? `/datasets/${datasetId}` : "",
    token,
    "Failed to load dataset."
  );
  const [fileState, setFileState] = useState({ loading: false, error: "", items: [] });

  const project = useMemo(() => {
    if (!dataset) {
      return null;
    }
    return projects.find((item) => item.project_id === dataset.project_id) || null;
  }, [projects, dataset]);

  useEffect(() => {
    let canceled = false;
    if (!token || !dataset || dataset.status === "committed") {
      setFileState({ loading: false, error: "", items: [] });
      return () => {
        canceled = true;
      };
    }

    setFileState((current) => ({ ...current, loading: true, error: "" }));
    fetchAllPages(`/datasets/${dataset.dataset_id}/files`, { token })
      .then((items) => {
        if (!canceled) {
          setFileState({ loading: false, error: "", items: Array.isArray(items) ? items : [] });
        }
      })
      .catch((err) => {
        if (!canceled) {
          setFileState({
            loading: false,
            error: err.message || "Failed to load dataset files.",
            items: [],
          });
        }
      });

    return () => {
      canceled = true;
    };
  }, [dataset, token]);

  const committedFiles = dataset?.commit_manifest?.files || [];
  const stagedFiles = fileState.items || [];
  const fileItems = dataset?.status === "committed" ? committedFiles : stagedFiles;

  return (
    <article className="card span-8">
      <div className="item-head">
        <h2>Dataset Detail</h2>
        {loading ? <span className="pill">Loading...</span> : null}
      </div>
      {error ? <p className="flash error">{error}</p> : null}

      {dataset ? (
        <div className="stack">
          <div className="inline">
            <span className="pill">{dataset.status}</span>
            {project ? <span className="pill">{project.name}</span> : null}
          </div>

          <div className="stack">
            <div className="subtle">Dataset ID</div>
            <div className="mono">{dataset.dataset_id}</div>
            <div className="subtle">Project ID</div>
            <div className="mono">{dataset.project_id}</div>
            <div className="subtle">Commit hash</div>
            <div className="mono">{dataset.commit_hash}</div>
            <div className="subtle">Created</div>
            <div className="mono">{formatDate(dataset.created_at)}</div>
            <div className="subtle">Updated</div>
            <div className="mono">{formatDate(dataset.updated_at)}</div>
          </div>

          <div className="stack">
            <div className="subtle">Question links</div>
            {(dataset.question_links || []).length === 0 ? (
              <p className="subtle">(none)</p>
            ) : (
              <div className="stack">
                {(dataset.question_links || []).map((link) => (
                  <div className="item" key={`${link.role}:${link.question_id}`}>
                    <div className="item-head">
                      <AppLink
                        to={`/app/questions/${link.question_id}`}
                        navigate={navigate}
                        className="link"
                      >
                        <strong>{link.role}</strong>
                      </AppLink>
                      <span className="pill">{link.outcome_status}</span>
                    </div>
                    <p className="mono">{link.question_id}</p>
                  </div>
                ))}
              </div>
            )}
          </div>

          <div className="stack">
            <div className="item-head">
              <h3>Files</h3>
              <span className="pill">
                {dataset.status === "committed" ? "committed" : "staged"} {fileItems.length}
              </span>
            </div>
            {dataset.status !== "committed" && fileState.loading ? (
              <p className="subtle">Loading attached files...</p>
            ) : null}
            {dataset.status !== "committed" && fileState.error ? (
              <p className="flash error">{fileState.error}</p>
            ) : null}
            {fileItems.length === 0 ? (
              <p className="subtle">(no files)</p>
            ) : (
              <div className="stack">
                {fileItems.map((file) => (
                  <div className="item" key={file.file_id || file.path}>
                    <div className="item-head">
                      <span className="mono">{file.path}</span>
                      <span className="subtle">{formatBytes(file.size_bytes)}</span>
                    </div>
                    <p className="mono">sha256: {file.checksum}</p>
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
      ) : null}

      <div className="inline detail-actions">
        <button type="button" className="btn-secondary" onClick={() => navigate("/app")}>
          Back
        </button>
        {dataset ? (
          <button
            type="button"
            className="btn-primary"
            onClick={() => {
              onSetActiveProject(dataset.project_id);
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

export { DatasetDetailCard, DatasetPanel };
