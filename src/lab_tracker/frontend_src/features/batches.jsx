import * as React from "react";

import { apiListRequest, apiRequest, buildApiPath } from "../shared/api.js";
import { formatDate } from "../shared/formatters.js";
import { StatusPill } from "../shared/ui.jsx";

const { useCallback, useEffect, useMemo, useState } = React;

const BATCH_CADENCE_OPTIONS = [
  { label: "Daily", value: "1440" },
  { label: "Every 12 hours", value: "720" },
  { label: "Weekly", value: "10080" },
];

function batchNoteCount(batch) {
  return batch?.source_note_count || batch?.source_note_ids?.length || 1;
}

function PendingBatchBanner({ enabled = true, token, navigate }) {
  const [batches, setBatches] = useState([]);
  const [batchTotal, setBatchTotal] = useState(0);

  useEffect(() => {
    let canceled = false;
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
      .catch(() => {
        if (!canceled) {
          setBatches([]);
          setBatchTotal(0);
        }
      });
    return () => {
      canceled = true;
    };
  }, [enabled, token]);

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

function BatchCards({ batches, emptyMessage, navigate }) {
  if (batches.length === 0) {
    return <p className="subtle">{emptyMessage}</p>;
  }
  return batches.map((batch) => (
    <article className="item" key={batch.change_set_id}>
      <div className="item-head">
        <strong className="summary-clamp">{batch.summary || "Pending review"}</strong>
        <StatusPill family="review" value={batch.status} />
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
  ));
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
  const [waitingBatches, setWaitingBatches] = useState([]);
  const [needsCommitBatches, setNeedsCommitBatches] = useState([]);
  const [runs, setRuns] = useState([]);
  const [settings, setSettings] = useState(null);
  const [loading, setLoading] = useState(false);
  const [enabled, setEnabled] = useState(true);
  const [cadenceMinutes, setCadenceMinutes] = useState("1440");
  const [runAtLocalTime, setRunAtLocalTime] = useState("18:00");
  const [timezoneName, setTimezoneName] = useState("America/New_York");

  const activeProject = useMemo(
    () => projects.find((project) => project.project_id === selectedProjectId) || null,
    [projects, selectedProjectId]
  );

  const loadBatches = useCallback(async () => {
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
      const runPath = buildApiPath("/batches/runs", {
        project_id: selectedProjectId,
        mine: true,
        limit: 20,
      });
      const [
        { data: batchData },
        { data: waitingData },
        { data: needsCommitData },
        { data: runData },
      ] = await Promise.all([
        apiListRequest(batchPath, { token }),
        apiListRequest(waitingPath, { token }),
        apiListRequest(needsCommitPath, { token }),
        apiListRequest(runPath, { token }),
      ]);
      setBatches(batchData || []);
      setWaitingBatches(waitingData || []);
      setNeedsCommitBatches(needsCommitData || []);
      setRuns(runData || []);
    } catch (err) {
      setFlash("", err.message || "Failed to load daily reviews.");
    } finally {
      setLoading(false);
    }
  }, [selectedProjectId, setFlash, token]);

  const loadSettings = useCallback(async () => {
    if (!selectedProjectId) {
      setSettings(null);
      return;
    }
    try {
      const nextSettings = await apiRequest(
        `/projects/${selectedProjectId}/graph-draft-batch-settings`,
        { token }
      );
      setSettings(nextSettings);
      setEnabled(Boolean(nextSettings.enabled));
      setCadenceMinutes(String(nextSettings.cadence_minutes || 1440));
      setRunAtLocalTime(nextSettings.run_at_local_time || "18:00");
      setTimezoneName(nextSettings.timezone_name || "America/New_York");
    } catch (err) {
      setSettings(null);
      setFlash("", err.message || "Failed to load batch cadence settings.");
    }
  }, [selectedProjectId, setFlash, token]);

  useEffect(() => {
    loadBatches();
  }, [loadBatches]);

  useEffect(() => {
    loadSettings();
  }, [loadSettings]);

  async function saveSettings(event) {
    event.preventDefault();
    if (!selectedProjectId || !canManageGraph) {
      return;
    }
    setBusy(true);
    setFlash("", "");
    try {
      const nextSettings = await apiRequest(
        `/projects/${selectedProjectId}/graph-draft-batch-settings`,
        {
          body: {
            cadence_minutes: Number(cadenceMinutes),
            enabled,
            run_at_local_time: runAtLocalTime,
            timezone_name: timezoneName,
          },
          method: "PATCH",
          token,
        }
      );
      setSettings(nextSettings);
      setFlash("Daily review schedule updated.");
    } catch (err) {
      setFlash("", err.message || "Failed to update batch cadence.");
    } finally {
      setBusy(false);
    }
  }

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
            <h3>Ready for you</h3>
            <BatchCards
              batches={batches}
              emptyMessage="No daily reviews need your response."
              navigate={navigate}
            />
            <h3>Waiting on others</h3>
            <BatchCards
              batches={waitingBatches}
              emptyMessage="Nothing is waiting for project review."
              navigate={navigate}
            />
            <h3>Needs commit</h3>
            <BatchCards
              batches={needsCommitBatches}
              emptyMessage="No submitted reviews need your approval."
              navigate={navigate}
            />
          </div>
        </section>

        <section className="review-pane">
          <div className="item-head">
            <h3>Your cadence</h3>
            {activeProject ? <span className="pill">{activeProject.name}</span> : null}
          </div>
          <form className="form" onSubmit={saveSettings}>
            <label className="inline toggle-row">
              <input
                type="checkbox"
                checked={enabled}
                disabled={!canManageGraph || !selectedProjectId}
                onChange={(event) => setEnabled(event.target.checked)}
              />
              Enabled
            </label>
            <label>
              Cadence
              <select
                value={cadenceMinutes}
                disabled={!canManageGraph || !selectedProjectId}
                onChange={(event) => setCadenceMinutes(event.target.value)}
              >
                {BATCH_CADENCE_OPTIONS.map((option) => (
                  <option key={option.value} value={option.value}>
                    {option.label}
                  </option>
                ))}
              </select>
            </label>
            <label>
              Local run time
              <input
                type="time"
                value={runAtLocalTime}
                disabled={!canManageGraph || !selectedProjectId}
                onChange={(event) => setRunAtLocalTime(event.target.value)}
              />
            </label>
            <div className="inline">
              <button
                type="button"
                className="btn-secondary"
                aria-pressed={runAtLocalTime === "18:00"}
                disabled={!canManageGraph || !selectedProjectId}
                onClick={() => setRunAtLocalTime("18:00")}
              >
                Evening (6:00 PM)
              </button>
              <button
                type="button"
                className="btn-secondary"
                aria-pressed={runAtLocalTime === "06:00"}
                disabled={!canManageGraph || !selectedProjectId}
                onClick={() => setRunAtLocalTime("06:00")}
              >
                Morning (6:00 AM)
              </button>
            </div>
            <p className="subtle">
              This is your schedule for the selected project. Most people pick evening to
              confirm the day&apos;s captures while the work is fresh, but mornings work too.
            </p>
            <label>
              Time zone
              <input
                value={timezoneName}
                disabled={!canManageGraph || !selectedProjectId}
                onChange={(event) => setTimezoneName(event.target.value)}
              />
            </label>
            {settings?.next_run_at ? (
              <p className="subtle">Next run: {formatDate(settings.next_run_at)}</p>
            ) : null}
            <div className="inline">
              <button className="btn-primary" disabled={!canManageGraph || !selectedProjectId}>
                Save cadence
              </button>
              <button
                type="button"
                className="btn-secondary"
                disabled={!canManageGraph || !selectedProjectId}
                onClick={runNow}
              >
                Run now
              </button>
            </div>
          </form>

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
