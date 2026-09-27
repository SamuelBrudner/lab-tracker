import * as React from "react";

import { useApiResource } from "../../hooks/useApiResource.js";
import { formatDate } from "../../shared/formatters.js";

const ADAPTER_LABELS = {
  "lab-tracker-client-figure": "Python figure capture",
  "lab-tracker-matlab-figure": "MATLAB figure capture",
  "lt-watch-files": "Watch folder",
  "lt-watch-acquisition": "Watch folder (acquisition)",
  "lt-watch-manifest": "Workflow manifest",
  "lt-watch": "Watch folder",
  "lt-git-snapshot": "Commit snapshot",
  "lt-repo": "Repository report",
  "lt-hpc": "HPC run",
  "lt-import-folder": "Folder import",
  mobile_capture: "Phone capture",
  share_target: "Phone share sheet",
};

const MANUAL_LABEL = "Typed in the app";

// A coverage capture source names an adapter, or only a provider, or nothing
// (typed in the app). The adapter is the most specific name it has.
function sourceLabel(source) {
  const adapter = source.evidence_adapter || source.evidence_source_provider;
  if (!adapter) {
    return MANUAL_LABEL;
  }
  return ADAPTER_LABELS[adapter] || adapter;
}

function describeSource(source) {
  const host = source.capture_host_label ? ` on ${source.capture_host_label}` : "";
  return `${sourceLabel(source)}${host}`;
}

function sourceKey(source) {
  return [
    source.evidence_source_provider,
    source.evidence_adapter,
    source.capture_install_id,
    source.capture_host_label,
  ].join("|");
}

// Automated capture fails quietly; this card makes the silence visible. It
// reads the project's coverage report and lists every capture path that
// delivered, flagging the ones that stopped, so a scientist sees a stalled
// scheduler or expired token before the review queue simply goes empty.
function CaptureHealthCard({ token, projectId }) {
  const { data, error, loading } = useApiResource(
    token && projectId ? `/projects/${projectId}/coverage` : "",
    token,
    "Capture health is unavailable."
  );
  if (!projectId) {
    return null;
  }
  const sources = data?.capture_sources || [];
  const quietCount = data?.quiet_source_count || 0;
  const recentDays = data?.recent_days;
  return (
    <article className="card span-12 capture-health" aria-labelledby="capture-health-title">
      <div className="item-head">
        <h2 id="capture-health-title">Capture health</h2>
        {loading ? <span className="pill">Loading...</span> : null}
        {!loading && data ? (
          <span className={`pill${quietCount ? " review-rejected" : ""}`}>
            {quietCount === 0
              ? `${sources.length} source${sources.length === 1 ? "" : "s"} active`
              : `${quietCount} gone quiet`}
          </span>
        ) : null}
      </div>
      {error ? <p className="subtle">{error}</p> : null}
      {data && sources.length === 0 ? (
        <p className="subtle">
          Nothing has been captured yet. A watch folder, figure capture, or the phone
          capture page turns bench work into staged notes without extra steps.
        </p>
      ) : null}
      {sources.length > 0 ? (
        <ul className="compact-list capture-health-list">
          {sources.map((source) => (
            <li key={sourceKey(source)} className={source.quiet ? "capture-health-quiet" : ""}>
              <strong>{describeSource(source)}</strong>
              <span className="subtle">
                {" "}
                · last capture {formatDate(source.last_capture_at)} ·{" "}
                {source.recent_note_count} in the last {recentDays} days
                {source.staged_unreviewed_count > 0
                  ? ` · ${source.staged_unreviewed_count} awaiting review`
                  : ""}
              </span>
              {source.quiet ? (
                <span className="pill review-rejected capture-health-flag">
                  quiet for over {recentDays} days
                </span>
              ) : null}
            </li>
          ))}
          {data?.capture_sources_truncated ? (
            <li className="subtle">Only the most recent sources are listed.</li>
          ) : null}
        </ul>
      ) : null}
    </article>
  );
}

export { CaptureHealthCard, sourceLabel };
