import * as React from "react";

import { apiListRequest, apiRequest, buildApiPath } from "../shared/api.js";
import { formatDate } from "../shared/formatters.js";
import { DailyReviewScheduleForm } from "./daily-review-schedule.jsx";

const { useCallback, useEffect, useMemo, useRef, useState } = React;

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
  const [batchTotal, setBatchTotal] = useState(0);
  const [loadError, setLoadError] = useState("");

  useEffect(() => {
    let canceled = false;
    setLoadError("");
    if (!enabled) {
      setBatches([]);
      setBatchTotal(0);
      return () => {
        canceled = true;
      };
    }
    apiListRequest(buildApiPath("/batches", { limit: 5, mine: true }), { token })
      .then(({ data, meta }) => {
        if (!canceled) {
          setBatches(data || []);
          setBatchTotal(Number(meta?.total ?? data?.length ?? 0));
        }
      })
      .catch((err) => {
        if (!canceled) {
          setBatches([]);
          setBatchTotal(0);
          setLoadError(
            `Could not load your daily reviews: ${err?.message || "request failed."}`
          );
        }
      });
    return () => {
      canceled = true;
    };
  }, [enabled, token]);

  if (loadError) {
    return (
      <p className="flash error" role="alert">
        {loadError}
      </p>
    );
  }
  if (batchTotal === 0 || batches.length === 0) {
    return null;
  }

  const meetingBatch = batches.find((batch) => (batch?.meeting_note_count || 0) > 0);
  const label = meetingBatch
    ? "A meeting is waiting to be fleshed out — review its scientific content"
    : batchTotal === 1
      ? "1 daily review ready"
      : `${batchTotal} daily reviews ready`;
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

// Drafts started from a single capture ("Draft graph update" on a note or an
// image) are reviewed on the same page as the batches so they are not lost
// between the Capture page's last-ten list and nowhere.
function isCaptureDraft(draft) {
  return draft?.draft_mode !== "graph_batch" && draft?.purpose !== "member_checkpoint_alignment";
}

function CaptureDraftCards({ drafts, emptyMessage, navigate }) {
  if (drafts.length === 0) {
    return <p className="subtle">{emptyMessage}</p>;
  }
  return drafts.map((draft) => (
    <article className="item" key={draft.change_set_id}>
      <div className="item-head">
        <strong className="summary-clamp">{draft.summary || "Draft from a capture"}</strong>
        <span className={pendingBatchStatus(draft.status)}>{draft.status}</span>
      </div>
      <div className="inline">
        <span className="pill">{formatDate(draft.created_at)}</span>
        {draft.model ? <span className="pill">{draft.model}</span> : null}
      </div>
      <button
        type="button"
        className="btn-primary"
        onClick={() =>
          navigate(`/app/graph-drafts/${draft.change_set_id}?return_to=${encodeURIComponent("/app/batches")}`)
        }
      >
        Review draft
      </button>
    </article>
  ));
}

function BatchCards({ batches, emptyMessage, navigate }) {
  if (batches.length === 0) {
    return <p className="subtle">{emptyMessage}</p>;
  }
  return batches.map((batch) => (
    <article className="item" key={batch.change_set_id}>
      <div className="item-head">
        <strong className="summary-clamp">{batch.summary || "Pending review"}</strong>
        <span className={pendingBatchStatus(batch.status)}>{batch.status}</span>
      </div>
      <div className="inline">
        <span className="pill">{formatDate(batch.created_at)}</span>
        <span className="pill">{batchNoteCount(batch)} notes</span>
        <span className="pill">{batch.operation_count ?? 0} ops</span>
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
  ));
}

function BatchReviewPage({
  token,
  projects,
  selectedProjectId,
  onSelectedProjectChange,
  navigate,
  canManageGraph,
  canManageProject = false,
  setBusy,
  setFlash,
}) {
  const [batches, setBatches] = useState([]);
  const [waitingBatches, setWaitingBatches] = useState([]);
  const [needsCommitBatches, setNeedsCommitBatches] = useState([]);
  const [unassignedOversightBatches, setUnassignedOversightBatches] = useState([]);
  const [runs, setRuns] = useState([]);
  const [captureDrafts, setCaptureDrafts] = useState([]);
  const [captureDraftsError, setCaptureDraftsError] = useState("");
  const [loading, setLoading] = useState(false);
  // Each load bumps the generation; results, failures and the loading reset of
  // a superseded load (e.g. for a previously selected project) are ignored.
  const loadGenerationRef = useRef(0);
  const captureDraftGenerationRef = useRef(0);
  // The project this page currently shows (null once unmounted). An async
  // action started for another project must not touch this page's state.
  const shownProjectIdRef = useRef(selectedProjectId);

  useEffect(() => {
    shownProjectIdRef.current = selectedProjectId;
    return () => {
      shownProjectIdRef.current = null;
    };
  }, [selectedProjectId]);

  const activeProject = useMemo(
    () => projects.find((project) => project.project_id === selectedProjectId) || null,
    [projects, selectedProjectId]
  );
  const waitingOnOthers = useMemo(() => {
    const needsCommitIds = new Set(
      needsCommitBatches.map((batch) => batch.change_set_id)
    );
    return waitingBatches.filter(
      (batch) => !needsCommitIds.has(batch.change_set_id)
    );
  }, [needsCommitBatches, waitingBatches]);

  const loadBatches = useCallback(async () => {
    const generation = ++loadGenerationRef.current;
    const isCurrent = () => generation === loadGenerationRef.current;
    setLoading(true);
    try {
      const batchPath = buildApiPath("/batches", {
        project_id: selectedProjectId,
        mine: true,
        limit: 100,
      });
      const waitingPath = buildApiPath("/batches", {
        project_id: selectedProjectId,
        mine: true,
        status: "submitted",
        limit: 100,
      });
      const needsCommitPath = buildApiPath("/batches", {
        project_id: selectedProjectId,
        needs_commit: true,
        limit: 100,
      });
      const unassignedOversightPath = buildApiPath("/batches", {
        project_id: selectedProjectId,
        unassigned_oversight: true,
        limit: 100,
      });
      const runPath = buildApiPath("/batches/runs", {
        project_id: selectedProjectId,
        mine: true,
        limit: 20,
      });
      const [
        { data: batchData },
        { data: waitingData },
        { data: needsCommitData },
        { data: unassignedOversightData },
        { data: runData },
      ] = await Promise.all([
        apiListRequest(batchPath, { token }),
        apiListRequest(waitingPath, { token }),
        apiListRequest(needsCommitPath, { token }),
        canManageProject || !selectedProjectId
          ? apiListRequest(unassignedOversightPath, { token })
          : Promise.resolve({ data: [] }),
        apiListRequest(runPath, { token }),
      ]);
      if (!isCurrent()) {
        return;
      }
      setBatches(batchData || []);
      setWaitingBatches(waitingData || []);
      setNeedsCommitBatches(needsCommitData || []);
      setUnassignedOversightBatches(unassignedOversightData || []);
      setRuns(runData || []);
    } catch (err) {
      if (isCurrent()) {
        setFlash("", err.message || "Failed to load daily reviews.");
      }
    } finally {
      if (isCurrent()) {
        setLoading(false);
      }
    }
  }, [canManageProject, selectedProjectId, setFlash, token]);

  useEffect(() => {
    loadBatches();
    return () => {
      // Invalidate the in-flight load on project change or unmount.
      loadGenerationRef.current += 1;
    };
  }, [loadBatches]);

  useEffect(() => {
    // Loaded apart from the batch queues so a failure here cannot hide them.
    const generation = ++captureDraftGenerationRef.current;
    setCaptureDraftsError("");
    Promise.all(
      ["ready", "changes_requested"].map((status) =>
        apiListRequest(
          buildApiPath("/graph-drafts", { project_id: selectedProjectId, status, limit: 50 }),
          { token }
        )
      )
    )
      .then((pages) => {
        if (generation !== captureDraftGenerationRef.current) {
          return;
        }
        const drafts = pages
          .flatMap((page) => page.data || [])
          .filter(isCaptureDraft)
          .sort((a, b) => (Date.parse(b.created_at || "") || 0) - (Date.parse(a.created_at || "") || 0));
        setCaptureDrafts(drafts);
      })
      .catch((err) => {
        if (generation === captureDraftGenerationRef.current) {
          setCaptureDrafts([]);
          setCaptureDraftsError(
            `Could not load drafts from captures: ${err?.message || "request failed."}`
          );
        }
      });
    return () => {
      captureDraftGenerationRef.current += 1;
    };
  }, [selectedProjectId, token]);

  async function runNow() {
    if (!selectedProjectId || !canManageGraph) {
      return;
    }
    const runProjectId = selectedProjectId;
    const runProjectName = activeProject?.name || "the previous project";
    setBusy(true);
    setFlash("", "");
    try {
      const run = await apiRequest("/batches/run-now", {
        body: { project_id: runProjectId },
        method: "POST",
        token,
      });
      // If the user moved to another project while the run (or the reload) was
      // in flight, this closure's loadBatches would replace that project's
      // queues with the old project's, and navigating would pull them away.
      const projectChanged = () => {
        if (shownProjectIdRef.current === runProjectId) {
          return false;
        }
        setFlash(`Daily review run for ${runProjectName} finished. Select it to see the results.`);
        return true;
      };
      if (projectChanged()) {
        return;
      }
      await loadBatches();
      if (projectChanged()) {
        return;
      }
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
            <h3>Ready for you</h3>
            <BatchCards
              batches={batches}
              emptyMessage="No daily reviews need your response."
              navigate={navigate}
            />
            <h3 className="review-drafts-from-captures">Drafts from your captures</h3>
            {captureDraftsError ? <p className="subtle">{captureDraftsError}</p> : null}
            <CaptureDraftCards
              drafts={captureDrafts}
              emptyMessage="No single-capture drafts are waiting for review."
              navigate={navigate}
            />
            <h3>Waiting on others</h3>
            <BatchCards
              batches={waitingOnOthers}
              emptyMessage="Nothing is waiting for project review."
              navigate={navigate}
            />
            <h3>Needs commit</h3>
            <BatchCards
              batches={needsCommitBatches}
              emptyMessage="No submitted reviews need your approval."
              navigate={navigate}
            />
            {canManageProject || !selectedProjectId ? (
              <>
                <h3>Unassigned project oversight</h3>
                <BatchCards
                  batches={unassignedOversightBatches}
                  emptyMessage="No legacy unassigned reviews need owner recovery."
                  navigate={navigate}
                />
              </>
            ) : null}
          </div>
        </section>

        <section className="review-pane">
          <div className="item-head">
            <h3>Your cadence</h3>
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
