import * as React from "react";

import { formatDate } from "../../shared/formatters.js";
import { useApiResource } from "../../hooks/useApiResource.js";

function VisualizationDetailCard({ token, vizId, navigate }) {
  const { data: viz, error, loading } = useApiResource(
    token && vizId ? `/visualizations/${vizId}` : "",
    token,
    "Failed to load visualization."
  );

  return (
    <article className="card span-8">
      <div className="item-head">
        <h2>Visualization Detail</h2>
        {loading ? <span className="pill">Loading...</span> : null}
      </div>
      {error ? <p className="flash error">{error}</p> : null}

      {viz ? (
        <div className="stack">
          <div className="inline">
            <span className="pill">{viz.viz_type}</span>
          </div>
          <div className="stack">
            <div className="subtle">Visualization ID</div>
            <div className="mono">{viz.viz_id}</div>
            <div className="subtle">Analysis ID</div>
            <div className="mono">{viz.analysis_id}</div>
            <div className="subtle">File path</div>
            <div className="mono">{viz.file_path}</div>
            <div className="subtle">Caption</div>
            <div>{viz.caption || <span className="subtle">(none)</span>}</div>
            <div className="subtle">Related claim IDs</div>
            {(viz.related_claim_ids || []).length === 0 ? (
              <div className="subtle">(none)</div>
            ) : (
              <div className="stack">
                {(viz.related_claim_ids || []).map((claimId) => (
                  <div className="mono" key={claimId}>
                    {claimId}
                  </div>
                ))}
              </div>
            )}
            <div className="subtle">Created</div>
            <div className="mono">{formatDate(viz.created_at)}</div>
            <div className="subtle">Updated</div>
            <div className="mono">{formatDate(viz.updated_at)}</div>
          </div>
        </div>
      ) : null}

      <div className="inline detail-actions">
        <button type="button" className="btn-secondary" onClick={() => navigate("/app")}>
          Back
        </button>
      </div>
    </article>
  );
}

export { VisualizationDetailCard };
