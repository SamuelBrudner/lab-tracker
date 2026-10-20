import * as React from "react";

import { apiListRequest, buildApiPath } from "../../shared/api.js";
import { graphDrafts } from "../../shared/gateways/index.js";
import { GraphDraftDetailCard } from "../graph-drafts.jsx";

const PAGE_SIZE = 5;
const ACTIVE = new Set(["ready", "changes_requested"]);
const RANK = { judgment: 0, confirmations: 1, later: 2 };

function CatchUpReview({ token, projectId, onClose, ...reviewProps }) {
  const [reviews, setReviews] = React.useState([]);
  const [offset, setOffset] = React.useState(0);
  const [hasMore, setHasMore] = React.useState(true);
  const [loading, setLoading] = React.useState(false);
  const [error, setError] = React.useState("");
  const [selectedId, setSelectedId] = React.useState("");
  const live = React.useRef(true);
  const pending = React.useRef(false);
  const epoch = React.useRef(0);

  const loadPage = React.useCallback(async (pageOffset) => {
    if (pending.current) {
      return;
    }
    pending.current = true;
    const generation = epoch.current;
    setLoading(true);
    setError("");
    try {
      const page = await apiListRequest(buildApiPath("/batches", {
        limit: PAGE_SIZE, mine: true, offset: pageOffset,
        ...(projectId ? { project_id: projectId } : {}),
      }), { token });
      if (!live.current || generation !== epoch.current) {
        return;
      }
      const details = await Promise.allSettled(page.data.map((item) => graphDrafts.getChangeSet(item.change_set_id, { token }, true)));
      if (!live.current || generation !== epoch.current) {
        return;
      }
      const failed = details.filter((result) => result.status === "rejected");
      const loaded = details.filter((result) => result.status === "fulfilled").map((result) => result.value);
      setReviews((current) => [...new Map([...current, ...loaded].map((item) => [item.change_set_id, item])).values()]);
      if (failed.length) {
        // Retry this page, retaining the successfully loaded reviews. Failure
        // never silently shrinks the outstanding count or claims completion.
        setError(`${failed.length} reviews could not be loaded. Retry to include them.`);
      } else {
        setOffset(pageOffset + page.data.length);
        setHasMore(pageOffset + page.data.length < (page.meta?.total ?? pageOffset + PAGE_SIZE + 1));
      }
    } catch (err) {
      if (live.current && generation === epoch.current) {
        setError(err.message || "Could not load your catch-up review.");
      }
    } finally {
      if (generation === epoch.current) {
        pending.current = false;
      }
      if (live.current && generation === epoch.current) {
        setLoading(false);
      }
    }
  }, [projectId, token]);

  React.useEffect(() => {
    live.current = true;
    epoch.current += 1;
    pending.current = false;
    setReviews([]);
    setOffset(0);
    setHasMore(true);
    setSelectedId("");
    setError("");
    loadPage(0);
    return () => { live.current = false; epoch.current += 1; };
  }, [loadPage]);

  const onReviewChange = React.useCallback((next) => {
    const generation = epoch.current;
    setReviews((current) => current.map((review) => review.change_set_id === next.change_set_id ? next : review));
    const carryover = next.review_metadata?.review_triage_carryover_to;
    if (carryover) {
      graphDrafts.getChangeSet(carryover, { token }, true).then((review) => {
        if (live.current && generation === epoch.current) {
          setReviews((current) => [...new Map([...current, review].map((item) => [item.change_set_id, item])).values()]);
        }
      }).catch(() => {
        if (live.current && generation === epoch.current) {
          setError("Kept decisions were committed; the follow-up review could not be loaded. Reopen the queue to continue it.");
        }
      });
    }
  }, [token]);

  const decisions = reviews.filter((review) => ACTIVE.has(review.status)).flatMap((review) =>
    (review.triage?.groups || []).filter((group) => !group.resolved).map((group) => ({ review, group, id: `${review.change_set_id}:${group.group_id}` }))
  ).sort((a, b) => RANK[a.group.lane] - RANK[b.group.lane] || Number(b.group.urgent) - Number(a.group.urgent) || String(a.review.created_at).localeCompare(String(b.review.created_at)));
  // A verdict saves review state. Keep a finalization step in this queue until
  // the contributor submits or the owner commits; otherwise the last verdict
  // would unmount its card before the person could finish the review.
  const finishing = reviews.filter((review) => ACTIVE.has(review.status) && review.triage?.groups &&
    review.triage.groups.every((group) => group.resolved)
  ).map((review) => ({ review, group: null, id: `${review.change_set_id}:finish` }));
  const queue = [...finishing, ...decisions];
  const selected = queue.find((decision) => decision.id === selectedId) || queue[0];

  return (
    <section className="review-catchup" aria-label="Catch-up review">
      <div className="item-head"><h3>Catch up on your decisions</h3><button type="button" className="btn-link" onClick={onClose}>Back to review queue</button></div>
      <p>{decisions.length} unresolved decisions across {reviews.length} loaded reviews.{finishing.length ? ` ${finishing.length} ${finishing.length === 1 ? "review is" : "reviews are"} ready to finish.` : ""}{hasMore ? " More reviews may be waiting." : ""}</p>
      {error ? <p className="flash error" role="alert">{error}</p> : null}
      {hasMore || error ? <button type="button" className="btn-secondary" disabled={loading} onClick={() => loadPage(offset)}>{loading ? "Loading reviews…" : error ? "Retry loading reviews" : "Include more reviews"}</button> : null}
      {selected ? <>
        <label>Choose a pending decision<select value={selected.id} onChange={(event) => setSelectedId(event.target.value)}>{queue.map((decision) => <option key={decision.id} value={decision.id}>{decision.group ? decision.group.lane === "later" ? "Can wait" : decision.group.lane === "confirmations" ? "Confirm" : "Judgment" : "Finish review"} · {String(decision.review.created_at || "").slice(0, 10)} · {decision.group?.statement || decision.group?.question || "Submit or commit saved decisions"}</option>)}</select></label>
        <GraphDraftDetailCard key={selected.id} {...reviewProps} token={token} changeSetId={selected.review.change_set_id} initialTriageGroupId={selected.group?.group_id || ""} onChangeSet={onReviewChange} backPath="/app/batches" />
      </> : !loading && !error ? <p role="status">No unresolved decisions in the loaded reviews.</p> : null}
    </section>
  );
}

export { CatchUpReview };
