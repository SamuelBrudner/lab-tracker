import * as React from "react";

import { apiRequest, buildApiPath, fetchAllPages } from "../shared/api.js";
import { formatBytes, formatDate } from "../shared/formatters.js";
import { AppLink } from "../shared/routing.jsx";
import { useApiResource } from "../hooks/useApiResource.js";

const { useCallback, useEffect, useMemo, useState } = React;

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

function ReviewPanel({
  token,
  user,
  projects,
  selectedProjectId,
  navigate,
  onFlash,
  onRefreshActiveProject,
}) {
  const [queueBusy, setQueueBusy] = useState(false);
  const [queueError, setQueueError] = useState("");
  const [pendingReviews, setPendingReviews] = useState([]);
  const [selectedReviewId, setSelectedReviewId] = useState("");
  const [selectedDatasetId, setSelectedDatasetId] = useState("");

  const [detailBusy, setDetailBusy] = useState(false);
  const [detailError, setDetailError] = useState("");
  const [detailWarnings, setDetailWarnings] = useState([]);
  const [dataset, setDataset] = useState(null);
  const [datasetFiles, setDatasetFiles] = useState([]);
  const [questionsById, setQuestionsById] = useState({});
  const [attachedNotes, setAttachedNotes] = useState([]);

  const [actionBusy, setActionBusy] = useState(false);
  const [actionError, setActionError] = useState("");
  const [reviewComment, setReviewComment] = useState("");

  const isAdmin = Boolean(user && user.role === "admin");

  const projectById = useMemo(() => {
    const index = {};
    (projects || []).forEach((project) => {
      index[project.project_id] = project;
    });
    return index;
  }, [projects]);

  const loadQueue = useCallback(
    async ({ keepSelection = true, selectionReviewId = "" } = {}) => {
      if (!token) {
        setPendingReviews([]);
        setSelectedReviewId("");
        setSelectedDatasetId("");
        setQueueError("");
        return;
      }
      setQueueBusy(true);
      setQueueError("");
      try {
        const items = await fetchAllPages("/reviews/pending", { token });
        setPendingReviews(items);

        const desiredSelection = keepSelection ? selectionReviewId : "";
        if (!desiredSelection) {
          const next = items[0] || null;
          setSelectedReviewId(next ? next.review_id : "");
          setSelectedDatasetId(next ? next.dataset_id : "");
          return;
        }

        const stillThere = items.some((review) => review.review_id === desiredSelection);
        if (!stillThere) {
          const next = items[0] || null;
          setSelectedReviewId(next ? next.review_id : "");
          setSelectedDatasetId(next ? next.dataset_id : "");
        }
      } catch (err) {
        setQueueError(err.message || "Failed to load review queue.");
        setPendingReviews([]);
        setSelectedReviewId("");
        setSelectedDatasetId("");
      } finally {
        setQueueBusy(false);
      }
    },
    [token]
  );

  useEffect(() => {
    loadQueue({ keepSelection: false });
  }, [loadQueue]);

  useEffect(() => {
    if (!selectedReviewId) {
      setSelectedDatasetId("");
      return;
    }
    const found = pendingReviews.find((review) => review.review_id === selectedReviewId) || null;
    setSelectedDatasetId(found ? found.dataset_id : "");
    setDetailError("");
    setDetailWarnings([]);
    setActionError("");
    setReviewComment("");
  }, [pendingReviews, selectedReviewId]);

  useEffect(() => {
    let canceled = false;
    if (!token || !selectedDatasetId) {
      setDataset(null);
      setDatasetFiles([]);
      setQuestionsById({});
      setAttachedNotes([]);
      setDetailError("");
      setDetailWarnings([]);
      return () => {
        canceled = true;
      };
    }

    async function loadDetail() {
      setDetailBusy(true);
      setDetailError("");
      setDetailWarnings([]);
      try {
        const loadedDataset = await apiRequest(`/datasets/${selectedDatasetId}`, { token });
        if (canceled) {
          return;
        }
        setDataset(loadedDataset);

        const warnings = [];

        let files = [];
        try {
          files = await fetchAllPages(`/datasets/${selectedDatasetId}/files`, { token });
        } catch (err) {
          warnings.push(err.message || "Dataset file list could not be loaded.");
        }
        if (!canceled) {
          setDatasetFiles(files);
        }

        const noteIds = loadedDataset?.commit_manifest?.note_ids || [];
        const manifestNotesSettled = Array.isArray(noteIds) && noteIds.length > 0
          ? await Promise.allSettled(noteIds.map((noteId) => apiRequest(`/notes/${noteId}`, { token })))
          : [];
        const manifestNotes = manifestNotesSettled
          .filter((result) => result.status === "fulfilled")
          .map((result) => result.value);
        if (manifestNotesSettled.some((result) => result.status !== "fulfilled")) {
          warnings.push("Some manifest-linked notes could not be loaded.");
        }

        const datasetProjectId = loadedDataset?.project_id || "";
        let datasetNotes = [];
        if (datasetProjectId) {
          try {
            datasetNotes = await fetchAllPages(
              buildApiPath("/notes", {
                project_id: datasetProjectId,
                target_entity_type: "dataset",
                target_entity_id: selectedDatasetId,
              }),
              { token }
            );
          } catch (err) {
            warnings.push(err.message || "Dataset note list could not be loaded.");
          }
        }

        const manifestNoteIds = new Set(
          (loadedDataset?.commit_manifest?.note_ids || []).map((value) => String(value))
        );
        const noteIndex = {};

        for (const note of manifestNotes) {
          if (!note || !note.note_id) {
            continue;
          }
          noteIndex[String(note.note_id)] = note;
        }

        for (const note of datasetNotes) {
          if (!note || !note.note_id) {
            continue;
          }
          const noteId = String(note.note_id);
          const targets = Array.isArray(note.targets) ? note.targets : [];
          const targetsDataset = targets.some(
            (target) =>
              target.entity_type === "dataset" && String(target.entity_id) === selectedDatasetId
          );
          if ((targetsDataset || manifestNoteIds.has(noteId)) && !noteIndex[noteId]) {
            noteIndex[noteId] = note;
          }
        }

        const mergedNotes = Object.values(noteIndex);
        mergedNotes.sort((a, b) => {
          const aTime = Date.parse(a.created_at || "") || 0;
          const bTime = Date.parse(b.created_at || "") || 0;
          return bTime - aTime;
        });
        if (!canceled) {
          setAttachedNotes(mergedNotes);
        }

        const questionIds = (loadedDataset?.question_links || []).map((link) => link.question_id);
        const questionSettled = await Promise.allSettled(
          questionIds.map((questionId) => apiRequest(`/questions/${questionId}`, { token }))
        );
        const nextQuestionsById = {};
        questionSettled.forEach((result) => {
          if (result.status !== "fulfilled") {
            return;
          }
          const question = result.value;
          if (!question || !question.question_id) {
            return;
          }
          nextQuestionsById[String(question.question_id)] = question;
        });
        if (questionSettled.some((result) => result.status !== "fulfilled")) {
          warnings.push("Some linked questions could not be loaded.");
        }
        if (!canceled) {
          setQuestionsById(nextQuestionsById);
          setDetailWarnings(warnings);
        }
      } catch (err) {
        if (!canceled) {
          setDetailError(err.message || "Failed to load review detail.");
          setDetailWarnings([]);
          setDataset(null);
          setDatasetFiles([]);
          setQuestionsById({});
          setAttachedNotes([]);
        }
      } finally {
        if (!canceled) {
          setDetailBusy(false);
        }
      }
    }

    loadDetail();

    return () => {
      canceled = true;
    };
  }, [selectedDatasetId, token]);

  async function handleResolve(action) {
    if (!token || !dataset || !dataset.dataset_id) {
      return;
    }
    if (!isAdmin) {
      setActionError("Only admins can resolve dataset reviews.");
      return;
    }

    const trimmed = reviewComment.trim();
    if (action === "request_changes" && !trimmed) {
      setActionError("Add a comment explaining the requested changes.");
      return;
    }

    setActionBusy(true);
    setActionError("");
    try {
      await apiRequest(`/datasets/${dataset.dataset_id}/review`, {
        method: "PATCH",
        token,
        body: {
          action,
          comments: trimmed || null,
        },
      });

      let message =
        action === "approve"
          ? "Dataset review approved."
          : action === "reject"
            ? "Dataset review rejected."
            : "Dataset review sent back for changes.";

      if (
        typeof onRefreshActiveProject === "function" &&
        selectedProjectId &&
        String(dataset.project_id) === String(selectedProjectId)
      ) {
        const refreshOutcome = await onRefreshActiveProject();
        if (refreshOutcome && refreshOutcome.ok === false && refreshOutcome.error) {
          message = `${message} Active project refresh failed: ${refreshOutcome.error}`;
        }
      }

      if (typeof onFlash === "function") {
        onFlash(message);
      }

      setReviewComment("");
      await loadQueue({ keepSelection: false });
    } catch (err) {
      setActionError(err.message || "Failed to resolve dataset review.");
    } finally {
      setActionBusy(false);
    }
  }

  const selectedProject = dataset ? projectById[dataset.project_id] : null;
  const selectedReview =
    pendingReviews.find((review) => review.review_id === selectedReviewId) || null;

  const reviewStatus = selectedReview?.status ? String(selectedReview.status) : "";
  const reviewStatusLabel = reviewStatus.replace(/_/g, " ");
  const reviewStatusClass = reviewStatus ? `pill review-status review-${reviewStatus}` : "";

  const manifest = dataset?.commit_manifest || null;
  const manifestPreview = useMemo(() => {
    if (!manifest) {
      return "";
    }
    try {
      return JSON.stringify(manifest, null, 2);
    } catch {
      return String(manifest);
    }
  }, [manifest]);

  return (
    <article className="card span-12">
      <div className="item-head">
        <h2>Dataset PR (Review Queue)</h2>
        <div className="inline">
          <span className="pill">assigned: {pendingReviews.length}</span>
          <button
            type="button"
            className="btn-secondary"
            disabled={!token || queueBusy}
            onClick={() => loadQueue({ keepSelection: true, selectionReviewId: selectedReviewId })}
          >
            Refresh
          </button>
        </div>
      </div>
      <p className="subtle">
        Pending dataset reviews assigned to you. Approving commits the dataset; requesting changes
        keeps it staged.
      </p>

      {queueError ? <p className="flash error">{queueError}</p> : null}

      <div className="dataset-pr-layout">
        <section className="review-pane">
          <div className="item-head">
            <h3>Queue</h3>
            {queueBusy ? <span className="pill">Loading...</span> : null}
          </div>

          {!token ? <p className="subtle">Sign in to view review assignments.</p> : null}

          {token && pendingReviews.length === 0 && !queueBusy ? (
            <p className="subtle">No pending reviews assigned.</p>
          ) : null}

          {pendingReviews.length > 0 ? (
            <div className="stack">
              {pendingReviews.map((review) => {
                const isSelected = review.review_id === selectedReviewId;
                const status = review?.status ? String(review.status) : "";
                const statusLabel = status.replace(/_/g, " ");
                const statusClass = status ? `pill review-status review-${status}` : "";
                return (
                  <button
                    key={review.review_id}
                    type="button"
                    className={`review-queue-item${isSelected ? " selected" : ""}`}
                    onClick={() => setSelectedReviewId(review.review_id)}
                  >
                    <div className="item-head">
                      <strong className="mono">{review.dataset_id}</strong>
                      {status ? <span className={statusClass}>{statusLabel}</span> : null}
                    </div>
                    <div className="subtle">Requested {formatDate(review.requested_at)}</div>
                  </button>
                );
              })}
            </div>
          ) : null}
        </section>

        <section className="review-pane">
          <div className="item-head">
            <h3>Detail</h3>
            <div className="inline">
              {detailBusy ? <span className="pill">Loading...</span> : null}
              {dataset ? (
                <AppLink
                  to={`/app/datasets/${dataset.dataset_id}`}
                  navigate={navigate}
                  className="link"
                >
                  Open dataset
                </AppLink>
              ) : null}
            </div>
          </div>

          {detailError ? <p className="flash error">{detailError}</p> : null}
          {detailWarnings.map((warning) => (
            <p className="warn" key={warning}>
              {warning}
            </p>
          ))}

          {dataset ? (
            <div className="stack">
              <div className="inline">
                <span className="pill">{dataset.status}</span>
                {selectedProject ? <span className="pill">{selectedProject.name}</span> : null}
                {reviewStatus ? <span className={reviewStatusClass}>{reviewStatusLabel}</span> : null}
              </div>

              <div className="dataset-pr-grid">
                <section className="review-pane">
                  <div className="item-head">
                    <h4>Files</h4>
                    <span className="pill">{datasetFiles.length}</span>
                  </div>
                  {datasetFiles.length === 0 ? (
                    <p className="subtle">(No attached files found.)</p>
                  ) : (
                    <div className="stack">
                      {datasetFiles.map((file) => (
                        <div className="item" key={file.file_id || file.path}>
                          <div className="item-head">
                            <span className="mono">{file.path}</span>
                            <span className="subtle">{formatBytes(file.size_bytes)}</span>
                          </div>
                          <p className="mono">sha256: {file.checksum}</p>
                          {file.file_id ? (
                            <a
                              className="link"
                              href={`/datasets/${dataset.dataset_id}/files/${file.file_id}/download`}
                            >
                              Download
                            </a>
                          ) : null}
                        </div>
                      ))}
                    </div>
                  )}
                </section>

                <section className="review-pane">
                  <div className="item-head">
                    <h4>Linked Questions</h4>
                    <span className="pill">{(dataset.question_links || []).length}</span>
                  </div>
                  {(dataset.question_links || []).length === 0 ? (
                    <p className="subtle">(No question links.)</p>
                  ) : (
                    <div className="stack">
                      {(dataset.question_links || []).map((link) => {
                        const question = questionsById[String(link.question_id)];
                        return (
                          <div className="item" key={`${link.role}:${link.question_id}`}>
                            <div className="item-head">
                              <strong>{link.role}</strong>
                              <span className="pill">{link.outcome_status}</span>
                            </div>
                            <p className="mono">{link.question_id}</p>
                            {question ? (
                              <p className="subtle">{String(question.text || "").slice(0, 160)}</p>
                            ) : null}
                            <AppLink
                              to={`/app/questions/${link.question_id}`}
                              navigate={navigate}
                              className="link"
                            >
                              Open question
                            </AppLink>
                          </div>
                        );
                      })}
                    </div>
                  )}
                </section>

                <section className="review-pane">
                  <div className="item-head">
                    <h4>Attached Notes</h4>
                    <span className="pill">{attachedNotes.length}</span>
                  </div>
                  {attachedNotes.length === 0 ? (
                    <p className="subtle">(No notes linked to this dataset.)</p>
                  ) : (
                    <div className="stack">
                      {attachedNotes.map((note) => (
                        <div className="item" key={note.note_id}>
                          <div className="item-head">
                            <span className="pill">{note.status}</span>
                            <span className="subtle">{formatDate(note.created_at)}</span>
                          </div>
                          <p className="mono">{note.note_id}</p>
                          <p className="subtle">
                            {(note.transcribed_text || note.raw_content || "(binary upload)").slice(0, 180)}
                          </p>
                          <AppLink to={`/app/notes/${note.note_id}`} navigate={navigate} className="link">
                            Open note
                          </AppLink>
                        </div>
                      ))}
                    </div>
                  )}
                </section>

                <section className="review-pane">
                  <div className="item-head">
                    <h4>Commit Manifest</h4>
                    <span className="pill mono">{dataset.commit_hash}</span>
                  </div>
                  {manifestPreview ? (
                    <pre className="mono manifest-preview">{manifestPreview}</pre>
                  ) : (
                    <p className="subtle">(No manifest.)</p>
                  )}
                </section>
              </div>

              {!isAdmin ? <p className="warn">Review actions require an admin account.</p> : null}

              {actionError ? <p className="flash error">{actionError}</p> : null}

              <form
                className="form"
                onSubmit={(event) => {
                  event.preventDefault();
                  handleResolve("request_changes");
                }}
              >
                <label>
                  Comment (required for request changes)
                  <textarea
                    value={reviewComment}
                    onChange={(event) => setReviewComment(event.target.value)}
                    disabled={!token || actionBusy}
                    placeholder="e.g. Please attach the rig log + add secondary question link."
                  />
                </label>

                <div className="inline">
                  <button
                    type="button"
                    className="btn-primary"
                    disabled={!token || !isAdmin || actionBusy}
                    onClick={() => handleResolve("approve")}
                  >
                    Approve
                  </button>
                  <button
                    type="submit"
                    className="btn-secondary"
                    disabled={!token || !isAdmin || actionBusy}
                  >
                    Request changes
                  </button>
                  <button
                    type="button"
                    className="btn-danger"
                    disabled={!token || !isAdmin || actionBusy}
                    onClick={() => handleResolve("reject")}
                  >
                    Reject
                  </button>
                </div>
              </form>
            </div>
          ) : token && selectedReviewId ? (
            <p className="subtle">Select a review to see details.</p>
          ) : (
            <p className="subtle">Pick a pending review from the queue.</p>
          )}
        </section>
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

export { DatasetDetailCard, DatasetPanel, ReviewPanel };
