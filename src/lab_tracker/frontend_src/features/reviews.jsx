import * as React from "react";

import { apiRequest, buildApiPath, fetchAllPages } from "../shared/api.js";
import { formatBytes, formatDate } from "../shared/formatters.js";
import { AppLink } from "../shared/routing.jsx";

const { useCallback, useEffect, useMemo, useState } = React;

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
        const manifestNotesSettled =
          Array.isArray(noteIds) && noteIds.length > 0
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

export { ReviewPanel };
