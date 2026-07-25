import * as React from "react";

import { apiListRequest, apiRequest, buildApiPath } from "../shared/api.js";
import { formatDate } from "../shared/formatters.js";
import { DailyReviewScheduleForm } from "./daily-review-schedule.jsx";

const { useCallback, useEffect, useMemo, useState } = React;

function batchNoteCount(batch) {
  return batch?.source_note_count || batch?.source_note_ids?.length || 1;
}

function pendingBatchStatus(status) {
  if (status === "ready" || status === "submitted" || status === "changes_requested") {
    return "pill review-pending";
  }
  if (status === "failed" || status === "rejected") {
    return "pill review-rejected";
  }
  return "pill";
}

function PendingBatchBanner({ enabled = true, token, navigate }) {
  const [batches, setBatches] = useState([]);

  useEffect(() => {
    let canceled = false;
    if (!enabled) {
      setBatches([]);
      return () => {
        canceled = true;
      };
    }
    apiListRequest(buildApiPath("/batches", { limit: 5 }), { token })
      .then(({ data }) => {
        if (!canceled) {
          setBatches(data || []);
        }
      })
      .catch(() => {
        if (!canceled) {
          setBatches([]);
        }
      });
    return () => {
      canceled = true;
    };
  }, [enabled, token]);

  if (batches.length === 0) {
    return null;
  }

  const meetingBatch = batches.find((batch) => (batch?.meeting_note_count || 0) > 0);
  const label = meetingBatch
    ? "A meeting is waiting to be fleshed out — review its scientific content"
    : batches.length === 1
      ? "1 daily review ready"
      : `${batches.length} daily reviews ready`;
  const firstBatch = meetingBatch || batches[0];
  return (
    <div className="flash ok batch-banner" role="status">
      <span>{label}</span>
      <button
        type="button"
        className="btn-secondary"
        onClick={() => navigate(`/app/batches/${firstBatch.change_set_id}`)}
      >
        Review
      </button>
      <button type="button" className="btn-secondary" onClick={() => navigate("/app/batches")}>
        View all
      </button>
    </div>
  );
}

function BatchReviewPage({
  token,
  projects,
  selectedProjectId,
  onSelectedProjectChange,
  navigate,
  canManageGraph,
  setBusy,
  setFlash,
}) {
  const [batches, setBatches] = useState([]);
  const [runs, setRuns] = useState([]);
  const [loading, setLoading] = useState(false);

  const activeProject = useMemo(
    () => projects.find((project) => project.project_id === selectedProjectId) || null,
    [projects, selectedProjectId]
  );

  const loadBatches = useCallback(async () => {
    setLoading(true);
    try {
      const batchPath = buildApiPath("/batches", {
        project_id: selectedProjectId,
        limit: 100,
      });
      const runPath = buildApiPath("/batches/runs", {
        project_id: selectedProjectId,
        limit: 20,
      });
      const [{ data: batchData }, { data: runData }] = await Promise.all([
        apiListRequest(batchPath, { token }),
        apiListRequest(runPath, { token }),
      ]);
      setBatches(batchData || []);
      setRuns(runData || []);
    } catch (err) {
      setFlash("", err.message || "Failed to load daily reviews.");
    } finally {
      setLoading(false);
    }
  }, [selectedProjectId, setFlash, token]);

  useEffect(() => {
    loadBatches();
  }, [loadBatches]);

  async function runNow() {
    if (!selectedProjectId || !canManageGraph) {
      return;
    }
    setBusy(true);
    setFlash("", "");
    try {
      const run = await apiRequest("/batches/run-now", {
        body: { project_id: selectedProjectId },
        method: "POST",
        token,
      });
      await loadBatches();
      if (run.change_set_id) {
        navigate(`/app/batches/${run.change_set_id}`);
      } else {
        setFlash(run.summary || "No staged notes found for this batch window.");
      }
    } catch (err) {
      setFlash("", err.message || "Failed to run the daily review.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <article className="card span-12">
      <div className="item-head">
        <h2>Daily review</h2>
        {loading ? <span className="pill">Loading...</span> : null}
      </div>

      <div className="review-layout">
        <section className="review-pane">
          <label>
            Project
            <select value={selectedProjectId || ""} onChange={onSelectedProjectChange}>
              <option value="">All accessible projects</option>
              {projects.map((project) => (
                <option key={project.project_id} value={project.project_id}>
                  {project.name}
                </option>
              ))}
            </select>
          </label>

          <div className="stack">
            {batches.length === 0 ? (
              <p className="subtle">No daily reviews waiting.</p>
            ) : (
              batches.map((batch) => (
                <article className="item" key={batch.change_set_id}>
                  <div className="item-head">
                    <strong className="summary-clamp">{batch.summary || "Pending review"}</strong>
                    <span className={pendingBatchStatus(batch.status)}>{batch.status}</span>
                  </div>
                  <div className="inline">
                    <span className="pill">{formatDate(batch.created_at)}</span>
                    <span className="pill">{batchNoteCount(batch)} notes</span>
                    <span className="pill">{(batch.operations || []).length} ops</span>
                    {batch.model ? <span className="pill">{batch.model}</span> : null}
                  </div>
                  <button
                    type="button"
                    className="btn-primary"
                    onClick={() => navigate(`/app/batches/${batch.change_set_id}`)}
                  >
                    Review batch
                  </button>
                </article>
              ))
            )}
          </div>
        </section>

        <section className="review-pane">
          <div className="item-head">
            <h3>Cadence</h3>
            {activeProject ? <span className="pill">{activeProject.name}</span> : null}
          </div>
          <DailyReviewScheduleForm
            token={token}
            projectId={selectedProjectId}
            canManage={canManageGraph}
            setBusy={setBusy}
            setFlash={setFlash}
            onRunNow={runNow}
          />

          <div className="stack">
            <h3>Recent Runs</h3>
            {runs.length === 0 ? (
              <p className="subtle">No batch runs recorded.</p>
            ) : (
              runs.map((run) => (
                <article className="item" key={run.run_id}>
                  <div className="item-head">
                    <strong>{run.status}</strong>
                    <span className="pill">{run.trigger}</span>
                  </div>
                  <div className="inline">
                    <span className="pill">{run.note_count} notes</span>
                    <span className="pill">{formatDate(run.window_end)}</span>
                  </div>
                  {run.summary ? <p className="summary-clamp">{run.summary}</p> : null}
                </article>
              ))
            )}
          </div>
        </section>
      </div>
    </article>
  );
}

export { BatchReviewPage, PendingBatchBanner };
