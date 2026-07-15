import * as React from "react";

import { apiListRequest, apiRequest, buildApiPath } from "../shared/api.js";
import { formatDate } from "../shared/formatters.js";

const { useCallback, useEffect, useMemo, useState } = React;

const BATCH_CADENCE_OPTIONS = [
  { label: "Daily", value: "1440" },
  { label: "Every 12 hours", value: "720" },
  { label: "Weekly", value: "10080" },
];

function batchNoteCount(batch) {
  return batch?.source_note_count || batch?.source_note_ids?.length || 1;
}

const DISPOSITION_CHIP_LABELS = [
  ["no_change", "no-change"],
  ["insufficient_info", "needs info"],
  ["not_presented", "not shown"],
];

function dispositionChipLabels(batch) {
  const counts = {};
  for (const entry of batch?.note_dispositions || []) {
    const key = entry?.disposition || "unknown";
    counts[key] = (counts[key] || 0) + 1;
  }
  return DISPOSITION_CHIP_LABELS.filter(([key]) => counts[key]).map(
    ([key, label]) => `${counts[key]} ${label}`,
  );
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
  const [settings, setSettings] = useState(null);
  const [loading, setLoading] = useState(false);
  const [enabled, setEnabled] = useState(true);
  const [cadenceMinutes, setCadenceMinutes] = useState("1440");
  const [runAtLocalTime, setRunAtLocalTime] = useState("18:00");
  const [timezoneName, setTimezoneName] = useState("America/New_York");
  const [defaultReviewerUserId, setDefaultReviewerUserId] = useState("");
  // Dirty flag (not value comparison): the settings snapshot and the field can
  // go stale independently across project switches, and PATCHing a stale
  // owner-gated value would silently transfer review authority.
  const [defaultReviewerDirty, setDefaultReviewerDirty] = useState(false);
  const [members, setMembers] = useState([]);

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
      setDefaultReviewerUserId(nextSettings.default_reviewer_user_id || "");
      setDefaultReviewerDirty(false);
    } catch (err) {
      setSettings(null);
      setFlash("", err.message || "Failed to load batch cadence settings.");
    }
  }, [selectedProjectId, setFlash, token]);

  const loadMembers = useCallback(async () => {
    if (!selectedProjectId) {
      setMembers([]);
      return;
    }
    try {
      const { data } = await apiListRequest(
        buildApiPath(`/projects/${selectedProjectId}/members`, { limit: 100 }),
        { token }
      );
      setMembers(data || []);
    } catch {
      setMembers([]);
    }
  }, [selectedProjectId, token]);

  useEffect(() => {
    loadBatches();
  }, [loadBatches]);

  useEffect(() => {
    loadSettings();
  }, [loadSettings]);

  useEffect(() => {
    loadMembers();
  }, [loadMembers]);

  async function saveSettings(event) {
    event.preventDefault();
    if (!selectedProjectId || !canManageGraph) {
      return;
    }
    setBusy(true);
    setFlash("", "");
    try {
      const body = {
        cadence_minutes: Number(cadenceMinutes),
        enabled,
        run_at_local_time: runAtLocalTime,
        timezone_name: timezoneName,
      };
      // Owner-gated field: only send it when the user actually touched it, so
      // contributors can still save cadence without tripping the owner gate.
      if (defaultReviewerDirty) {
        if (defaultReviewerUserId) {
          body.default_reviewer_user_id = defaultReviewerUserId;
        } else {
          body.clear_default_reviewer = true;
        }
      }
      const nextSettings = await apiRequest(
        `/projects/${selectedProjectId}/graph-draft-batch-settings`,
        {
          body,
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
                    {dispositionChipLabels(batch).map((label) => (
                      <span className="pill" key={label}>
                        {label}
                      </span>
                    ))}
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
              The server poller checks for due projects; this setting decides when this
              project becomes due. Most labs pick evening to confirm the day&apos;s captures
              while the work is fresh, but mornings work too.
            </p>
            <label>
              Time zone
              <input
                value={timezoneName}
                disabled={!canManageGraph || !selectedProjectId}
                onChange={(event) => setTimezoneName(event.target.value)}
              />
            </label>
            <label>
              Fallback reviewer
              <select
                value={defaultReviewerUserId}
                disabled={!canManageGraph || !selectedProjectId}
                onChange={(event) => {
                  setDefaultReviewerUserId(event.target.value);
                  setDefaultReviewerDirty(true);
                }}
              >
                <option value="">Earliest project owner (automatic)</option>
                {members.map((member) => (
                  <option key={member.user_id} value={member.user_id}>
                    {member.username || member.user_id}
                  </option>
                ))}
              </select>
            </label>
            <p className="subtle">
              Captures that route to nobody — unattributed, or from someone who left the
              project — land in this reviewer&apos;s daily review instead of being dropped.
              Changing it is owner-only.
            </p>
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
