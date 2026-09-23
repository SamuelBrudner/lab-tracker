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
  manual: "Typed in the app",
};

function adapterLabel(adapter) {
  return ADAPTER_LABELS[adapter] || adapter;
}

function describeSource(source) {
  const host = source.host_label ? ` on ${source.host_label}` : "";
  return `${adapterLabel(source.adapter)}${host}`;
}

// Automated capture fails quietly; this card makes the silence visible. It
// lists every capture path that delivered in the window and flags the ones
// that stopped, so a scientist sees a stalled scheduler or expired token
// before the review queue simply goes empty.
function CaptureHealthCard({ token, projectId }) {
  const { data, error, loading } = useApiResource(
    token && projectId ? `/projects/${projectId}/capture-health` : "",
    token,
    "Capture health is unavailable."
  );
  if (!projectId) {
    return null;
  }
  const sources = data?.sources || [];
  const quiet = sources.filter((source) => source.quiet);
  return (
    <article className="card span-12 capture-health" aria-labelledby="capture-health-title">
      <div className="item-head">
        <h2 id="capture-health-title">Capture health</h2>
        {loading ? <span className="pill">Loading...</span> : null}
        {!loading && data ? (
          <span className={`pill${quiet.length ? " review-rejected" : ""}`}>
            {quiet.length === 0
              ? `${sources.length} source${sources.length === 1 ? "" : "s"} active`
              : `${quiet.length} gone quiet`}
          </span>
        ) : null}
      </div>
      {error ? <p className="subtle">{error}</p> : null}
      {data && sources.length === 0 ? (
        <p className="subtle">
          Nothing was captured in the last {data.window_days} days. A watch folder,
          figure capture, or the phone capture page turns bench work into staged notes
          without extra steps.
        </p>
      ) : null}
      {sources.length > 0 ? (
        <ul className="compact-list capture-health-list">
          {sources.map((source) => (
            <li
              key={`${source.adapter}|${source.host_label}`}
              className={source.quiet ? "capture-health-quiet" : ""}
            >
              <strong>{describeSource(source)}</strong>
              <span className="subtle">
                {" "}
                · last capture {formatDate(source.last_captured_at)} ·{" "}
                {source.captured_recent} in the last {data.recent_days} days
                {source.staged_unreviewed > 0
                  ? ` · ${source.staged_unreviewed} awaiting review`
                  : ""}
              </span>
              {source.quiet ? (
                <span className="pill review-rejected capture-health-flag">
                  quiet for over {data.recent_days} days
                </span>
              ) : null}
            </li>
          ))}
        </ul>
      ) : null}
    </article>
  );
}

export { CaptureHealthCard, adapterLabel };
