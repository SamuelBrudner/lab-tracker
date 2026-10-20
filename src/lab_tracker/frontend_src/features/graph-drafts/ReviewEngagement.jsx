import * as React from "react";
import { apiRequest } from "../../shared/api.js";

function ReviewEngagement({ token, projectId }) {
  const [open, setOpen] = React.useState(false);
  const [report, setReport] = React.useState(null);
  const [error, setError] = React.useState("");
  React.useEffect(() => {
    if (!open) {
      return;
    }
    let current = true;
    setReport(null);
    setError("");
    apiRequest(`/projects/${projectId}/review-engagement`, { token }).then((next) => {
      if (current) { setReport(next); }
    }).catch((err) => {
      if (current) { setError(err.message || "Could not load review participation."); }
    });
    return () => { current = false; };
  }, [open, projectId, token]);
  return (
    <details className="context-details" onToggle={(event) => setOpen(event.currentTarget.open)}>
      <summary>Review participation over the last 30 days</summary>
      {error ? <p role="alert">{error}</p> : !report ? <p>Loading participation…</p> : (
        <div className="triage-participation">
          <table><thead><tr><th>Reviewer</th><th>Assigned</th><th>Opened</th><th>Triage started</th><th>Submitted</th><th>Committed</th><th>Waiting</th></tr></thead>
            <tbody>{report.reviewers.map((reviewer) => <tr key={reviewer.reviewer_user_id || "unassigned"}><td>{reviewer.reviewer}</td><td>{reviewer.assigned}</td><td>{report.opening_counts_available ? reviewer.opened : "—"}</td><td>{reviewer.decisions_started}</td><td>{reviewer.submitted}</td><td>{reviewer.committed}</td><td>{reviewer.waiting}{reviewer.followups_pending ? ` + ${reviewer.followups_pending} follow-ups` : ""}</td></tr>)}</tbody>
          </table>
          {report.reviewers.length === 0 ? <p>No daily reviews in this window.</p> : null}
          <p className="subtle">Triage starts are recorded after the triage update. Background loading does not count as opening a review. Opening counts need usage-event logging enabled.</p>
          {report.reviewers.filter((reviewer) => reviewer.median_seconds_to_finish !== null).map((reviewer) => <p className="subtle" key={reviewer.reviewer_user_id}>Median elapsed time from opening to finishing for {reviewer.reviewer}: {Math.round(reviewer.median_seconds_to_finish)} seconds.</p>)}
        </div>
      )}
    </details>
  );
}

export { ReviewEngagement };
