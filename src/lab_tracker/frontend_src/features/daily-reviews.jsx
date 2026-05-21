import * as React from "react";

import { apiRequest } from "../shared/api.js";
import { formatDate } from "../shared/formatters.js";

const { useCallback, useEffect, useMemo, useState } = React;

function statusClass(status) {
  if (status === "reviewed" || status === "ready") {
    return "pill review-approved";
  }
  if (status === "failed") {
    return "pill review-rejected";
  }
  return "pill review-pending";
}

function draftStatusClass(status) {
  if (status === "committed" || status === "ready") {
    return "pill review-approved";
  }
  if (status === "failed") {
    return "pill review-rejected";
  }
  return "pill review-pending";
}

function operationCounts(changeSet) {
  const counts = {
    accepted: 0,
    applied: 0,
    proposed: 0,
    rejected: 0,
  };
  for (const operation of changeSet?.operations || []) {
    if (Object.prototype.hasOwnProperty.call(counts, operation.status)) {
      counts[operation.status] += 1;
    }
  }
  return counts;
}

function hasText(value) {
  return typeof value === "string" && value.trim().length > 0;
}

function DraftBrief({ brief }) {
  if (!brief) {
    return null;
  }
  return (
    <div>
      {hasText(brief.headline) ? <strong>{brief.headline}</strong> : null}
      {hasText(brief.interpretation) ? <p>{brief.interpretation}</p> : null}
      {hasText(brief.recommended_action) ? (
        <p>
          <strong>Recommended action:</strong> {brief.recommended_action}
        </p>
      ) : null}
      {brief.risks?.length ? (
        <div>
          <strong>Risks</strong>
          <ul>
            {brief.risks.map((risk) => (
              <li key={risk}>{risk}</li>
            ))}
          </ul>
        </div>
      ) : null}
      {brief.questions_for_reviewer?.length ? (
        <div>
          <strong>Questions</strong>
          <ul>
            {brief.questions_for_reviewer.map((question) => (
              <li key={question}>{question}</li>
            ))}
          </ul>
        </div>
      ) : null}
    </div>
  );
}

function DailyGraphReviewCard({ token, reviewId, navigate, canWrite, setBusy, setFlash }) {
  const [review, setReview] = useState(null);
  const [changeSets, setChangeSets] = useState([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  const readyDrafts = useMemo(
    () => changeSets.filter((changeSet) => changeSet.status === "ready").length,
    [changeSets]
  );
  const reviewBrief = review?.review_brief && Object.keys(review.review_brief).length
    ? review.review_brief
    : null;
  const briefByChangeSet = useMemo(() => {
    const summaries = new Map();
    for (const summary of review?.review_brief?.draft_summaries || []) {
      if (summary?.change_set_id) {
        summaries.set(summary.change_set_id, summary);
      }
    }
    return summaries;
  }, [review]);

  const loadReview = useCallback(async () => {
    if (!reviewId) {
      return;
    }
    setLoading(true);
    setError("");
    try {
      const nextReview = await apiRequest(`/daily-graph-reviews/${reviewId}`, { token });
      setReview(nextReview);
      const nextChangeSets = await Promise.all(
        (nextReview?.change_set_ids || []).map((changeSetId) =>
          apiRequest(`/graph-drafts/${changeSetId}`, { token })
        )
      );
      setChangeSets(nextChangeSets.filter(Boolean));
    } catch (err) {
      setError(err.message || "Failed to load daily graph review.");
    } finally {
      setLoading(false);
    }
  }, [reviewId, token]);

  useEffect(() => {
    loadReview();
  }, [loadReview]);

  async function markReviewed() {
    if (!review || !canWrite) {
      return;
    }
    setBusy(true);
    setFlash("", "");
    try {
      const nextReview = await apiRequest(`/daily-graph-reviews/${review.review_id}/reviewed`, {
        method: "POST",
        token,
      });
      setReview(nextReview);
      setFlash("Daily graph review marked reviewed.");
    } catch (err) {
      setFlash("", err.message || "Failed to mark daily graph review reviewed.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <article className="card span-12">
      <div className="item-head">
        <h2>Daily Graph Review</h2>
        {loading ? <span className="pill">Loading...</span> : null}
      </div>
      {error ? <p className="flash error">{error}</p> : null}

      {review ? (
        <div className="stack">
          <section className="item">
            <div className="inline">
              <span className={statusClass(review.status)}>{review.status}</span>
              <span className="pill">{readyDrafts} ready drafts</span>
              <span className="pill">{changeSets.length} total drafts</span>
            </div>
            <p>{review.summary || "No review summary was stored."}</p>
            <div className="inline">
              <span className="mono">{formatDate(review.window_start)}</span>
              <span className="subtle">to</span>
              <span className="mono">{formatDate(review.window_end)}</span>
            </div>
            {review.error_metadata?.source_errors?.length ? (
              <div className="flash error">
                {review.error_metadata.source_errors.length} source note
                {review.error_metadata.source_errors.length === 1 ? "" : "s"} could not be drafted.
              </div>
            ) : null}
            {review.error_metadata?.review_brief_error ? (
              <div className="flash error">
                Review brief deferred: {review.error_metadata.review_brief_error}
              </div>
            ) : null}
          </section>

          {reviewBrief ? (
            <section className="item">
              <div className="item-head">
                <h3>{reviewBrief.title || "Review Brief"}</h3>
              </div>
              {hasText(reviewBrief.executive_summary) ? (
                <p>{reviewBrief.executive_summary}</p>
              ) : null}
              {reviewBrief.review_priorities?.length ? (
                <div>
                  <strong>Review priorities</strong>
                  <ul>
                    {reviewBrief.review_priorities.map((priority) => (
                      <li key={priority}>{priority}</li>
                    ))}
                  </ul>
                </div>
              ) : null}
            </section>
          ) : (
            <p className="subtle">No daily review brief is stored for this review.</p>
          )}

          {changeSets.length ? (
            changeSets.map((changeSet) => {
              const counts = operationCounts(changeSet);
              const brief = briefByChangeSet.get(changeSet.change_set_id);
              return (
                <article className="item" key={changeSet.change_set_id}>
                  <div className="item-head">
                    <strong>{changeSet.source_filename || "Graph draft"}</strong>
                    <span className={draftStatusClass(changeSet.status)}>{changeSet.status}</span>
                  </div>
                  <div className="inline">
                    <span className="pill">{changeSet.prompt_version}</span>
                    <span className="pill">{changeSet.operations?.length || 0} operations</span>
                    <span className="pill">{counts.proposed} proposed</span>
                    <span className="pill">{counts.accepted} accepted</span>
                    <span className="pill">{counts.applied} applied</span>
                    <span className="pill">{counts.rejected} rejected</span>
                  </div>
                  <p className="mono">{changeSet.source_note_id}</p>
                  {changeSet.error_metadata?.message ? (
                    <p className="flash error">{changeSet.error_metadata.message}</p>
                  ) : null}
                  <DraftBrief brief={brief} />
                  <div className="inline">
                    <button
                      type="button"
                      className="btn-primary"
                      onClick={() =>
                        navigate(`/app/graph-drafts/${changeSet.change_set_id}`)
                      }
                    >
                      Review draft
                    </button>
                    <button
                      type="button"
                      className="btn-secondary"
                      onClick={() => navigate(`/app/notes/${changeSet.source_note_id}`)}
                    >
                      Source note
                    </button>
                  </div>
                </article>
              );
            })
          ) : (
            <p className="subtle">No graph drafts were linked to this daily review.</p>
          )}

          <div className="inline detail-actions">
            <button type="button" className="btn-secondary" onClick={() => navigate("/app")}>
              Back
            </button>
            <button
              type="button"
              className="btn-primary"
              disabled={!canWrite || review.status === "reviewed"}
              onClick={markReviewed}
            >
              Mark reviewed
            </button>
          </div>
        </div>
      ) : null}
    </article>
  );
}

export { DailyGraphReviewCard };
