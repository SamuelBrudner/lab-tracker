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
};

// Typed notes and phone or share-sheet captures carry no adapter, so the
// coverage read groups them together; nothing monitors their pace.
const MANUAL_LABEL = "Typed or phone captures (not monitored)";
const UNAVAILABLE_MESSAGE = "Capture health is unavailable.";
const BEHIND_LABEL = "client behind";
const TRUNCATED_MESSAGE =
  "Only the most recent sources are listed; the quiet count covers every source.";

// A coverage capture source names an adapter, or only a provider, or nothing
// (typed or phone captures). The adapter is the most specific name it has.
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

// The server's own notice when it wrote one; otherwise say which releases differ.
function behindDescription(source, serverRelease) {
  if (source.update_notice) {
    return source.update_notice;
  }
  const client = source.capture_client_version || "an unreported release";
  const server = serverRelease?.version || "an unknown release";
  return `Captured with release ${client}; this server runs release ${server}.`;
}

function sourceKey(source) {
  return [
    source.evidence_source_provider,
    source.evidence_adapter,
    source.capture_install_id,
    source.capture_host_label,
  ].join("|");
}

// Scheduled capture fails quietly; this card makes the silence visible. It
// reads the project's coverage report and lists every capture path that
// delivered, flagging the scheduled ones (watch folders, HPC runs) that
// stopped, so a scientist sees a stalled scheduler or expired token before
// the review queue simply goes empty. A source whose client runs a release
// behind the server carries a "client behind" pill, so a quiet watcher and
// its outdated install read as one machine.
function CaptureHealthCard({ token, projectId }) {
  const { data, error, loading } = useApiResource(
    projectId ? `/projects/${projectId}/coverage` : "",
    token,
    UNAVAILABLE_MESSAGE
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
      {error ? (
        <p className="subtle">
          {UNAVAILABLE_MESSAGE}
          {error !== UNAVAILABLE_MESSAGE ? ` ${error}` : ""}
        </p>
      ) : null}
      {data && sources.length === 0 ? (
        <p className="subtle">
          Nothing has been captured yet. A watch folder, an HPC run, or figure capture
          turns bench work into staged notes without extra steps, and this card shows
          when a scheduled one stops delivering.
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
              {source.release_status === "behind" ? (
                <span
                  className="pill capture-health-flag capture-health-behind"
                  title={behindDescription(source, data?.server_release)}
                  aria-label={`${BEHIND_LABEL}: ${behindDescription(source, data?.server_release)}`}
                >
                  {BEHIND_LABEL}
                </span>
              ) : null}
            </li>
          ))}
          {data?.capture_sources_truncated ? (
            <li className="subtle">{TRUNCATED_MESSAGE}</li>
          ) : null}
        </ul>
      ) : null}
    </article>
  );
}

export { CaptureHealthCard, sourceLabel };
