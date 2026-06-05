import * as React from "react";

import { formatDate } from "../../shared/formatters.js";
import { useApiResource } from "../../hooks/useApiResource.js";

function groupLinksByRelation(links = []) {
  return links.reduce((groups, link) => {
    const key = [link.relation, link.slot || ""].join(":");
    const items = groups[key] || [];
    items.push(link);
    groups[key] = items;
    return groups;
  }, {});
}

function GoalDetailCard({ token, goalId, navigate }) {
  const { data: goal, error, loading } = useApiResource(
    goalId ? `/goals/${goalId}` : "",
    token,
    "Failed to load goal."
  );
  const groupedLinks = React.useMemo(() => groupLinksByRelation(goal?.links || []), [goal]);

  return (
    <article className="card span-8">
      <div className="item-head">
        <h2>Goal Detail</h2>
        {loading ? <span className="pill">Loading...</span> : null}
      </div>
      {error ? <p className="flash error">{error}</p> : null}

      {goal ? (
        <div className="stack">
          <div className="inline">
            <span className="pill">{goal.goal_type}</span>
            <span className="pill">{goal.status}</span>
          </div>
          <div className="stack">
            <div className="subtle">Title</div>
            <div>{goal.title}</div>
            <div className="subtle">Goal ID</div>
            <div className="mono">{goal.goal_id}</div>
            <div className="subtle">Project ID</div>
            <div className="mono">{goal.project_id}</div>
            <div className="subtle">Summary</div>
            <div>{goal.summary || <span className="subtle">(none)</span>}</div>
            <div className="subtle">Target date</div>
            <div className="mono">{goal.target_date || "(none)"}</div>
            <div className="subtle">External reference</div>
            <div className="mono">{goal.external_ref || "(none)"}</div>
            <div className="subtle">Attributes</div>
            {Object.keys(goal.attributes || {}).length === 0 ? (
              <div className="subtle">(none)</div>
            ) : (
              <pre className="mono">{JSON.stringify(goal.attributes, null, 2)}</pre>
            )}
            <div className="subtle">Links</div>
            {(goal.links || []).length === 0 ? (
              <div className="subtle">(none)</div>
            ) : (
              <div className="stack">
                {Object.entries(groupedLinks).map(([groupKey, links]) => (
                  <div className="stack" key={groupKey}>
                    <strong>{groupKey.replace(":", " / ")}</strong>
                    {links.map((link) => (
                      <div className="mono" key={link.link_id}>
                        {link.link_status} {link.target.entity_type}:{link.target.entity_id}
                      </div>
                    ))}
                  </div>
                ))}
              </div>
            )}
            <div className="subtle">Created</div>
            <div className="mono">{formatDate(goal.created_at)}</div>
            <div className="subtle">Updated</div>
            <div className="mono">{formatDate(goal.updated_at)}</div>
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

export { GoalDetailCard };
