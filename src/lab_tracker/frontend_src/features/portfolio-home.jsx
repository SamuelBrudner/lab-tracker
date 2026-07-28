import * as React from "react";

import { apiListRequest, buildApiPath } from "../shared/api.js";
import { formatDate } from "../shared/formatters.js";
import { StatusPill } from "../shared/ui.jsx";

const { useCallback, useEffect, useMemo, useState } = React;

function groupLabel(group) {
  return group?.project_group?.name || "Ungrouped projects";
}

function plural(count, singular, pluralLabel = `${singular}s`) {
  return count === 1 ? `1 ${singular}` : `${count} ${pluralLabel}`;
}

function ownersLabel(project) {
  const owners = project?.owners || [];
  if (owners.length === 0) {
    return "No owner listed";
  }
  return owners.map((owner) => owner.username || owner.user_id).join(", ");
}

function projectRows(groups) {
  return groups.flatMap((group) => group.projects || []);
}

function PortfolioHome({ authEnabled, enabled, token, onOpenProject }) {
  const [groups, setGroups] = useState([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  const rows = useMemo(() => projectRows(groups), [groups]);
  const triageFlagCount = useMemo(
    () => rows.reduce((total, project) => total + (project.triage_flags || []).length, 0),
    [rows]
  );

  const loadPortfolio = useCallback(async () => {
    if (!enabled) {
      setGroups([]);
      return;
    }
    setLoading(true);
    setError("");
    try {
      const { data } = await apiListRequest(
        buildApiPath("/portfolio/summary", { limit: 100, offset: 0 }),
        { token }
      );
      setGroups(data || []);
    } catch (err) {
      setError(err.message || "Failed to load portfolio summary.");
    } finally {
      setLoading(false);
    }
  }, [enabled, token]);

  useEffect(() => {
    loadPortfolio();
  }, [loadPortfolio]);

  if (!enabled) {
    return null;
  }

  return (
    <article className="card span-12">
      <div className="item-head">
        <h2>Portfolio Home</h2>
        {loading ? <span className="pill">Loading...</span> : null}
      </div>
      {!authEnabled ? (
        <p className="warn">
          Local single-user mode: per-trainee differentiation is unavailable until multi-user auth
          is enabled.
        </p>
      ) : null}
      {error ? <p className="flash error">{error}</p> : null}
      <div className="inline">
        <div className="kpi">
          <span className="subtle">Projects</span>
          <strong>{rows.length}</strong>
        </div>
        <div className="kpi">
          <span className="subtle">Triage flags</span>
          <strong>{triageFlagCount}</strong>
        </div>
        <button type="button" className="btn-secondary" onClick={loadPortfolio}>
          Refresh
        </button>
      </div>

      <div className="stack detail-actions">
        {groups.length === 0 && !loading ? (
          <p className="subtle">No portfolio rows available.</p>
        ) : (
          groups.map((group) => (
            <section className="stack" key={group.project_group?.group_id || "ungrouped"}>
              <div className="item-head">
                <h3>{groupLabel(group)}</h3>
                <span className="pill">{plural(group.project_count || 0, "project")}</span>
              </div>
              {(group.projects || []).map((project) => (
                <article className="item" key={project.project_id}>
                  <div className="item-head">
                    <strong>{project.name}</strong>
                    <span className="pill">{project.status}</span>
                  </div>
                  <div className="inline">
                    <span className="pill">
                      {plural(project.open_question_count || 0, "open question")}
                    </span>
                    <span className="pill">
                      {plural(project.draft_dataset_count || 0, "draft dataset")}
                    </span>
                    <span className="pill">
                      {plural(project.committed_dataset_count || 0, "committed dataset")}
                    </span>
                    <span className="pill">
                      {plural(project.staged_analysis_count || 0, "staged analysis", "staged analyses")}
                    </span>
                    <span className="pill">
                      {plural(project.unreviewed_claim_count || 0, "unreviewed claim")}
                    </span>
                  </div>
                  <div className="inline">
                    {(project.triage_flags || []).length === 0 ? (
                      <StatusPill family="review" value="accepted">No triage flags</StatusPill>
                    ) : (
                      project.triage_flags.map((flag) => (
                        <StatusPill family="flag" key={flag.key} value={flag.severity}>
                          {flag.label}: {flag.count}
                        </StatusPill>
                      ))
                    )}
                  </div>
                  <p className="subtle">Owners: {ownersLabel(project)}</p>
                  <p className="subtle">
                    Last activity: {project.last_activity_at ? formatDate(project.last_activity_at) : "none"}
                  </p>
                  <button
                    type="button"
                    className="btn-secondary"
                    onClick={() => onOpenProject(project.project_id)}
                  >
                    Open project
                  </button>
                </article>
              ))}
            </section>
          ))
        )}
      </div>
    </article>
  );
}

export { PortfolioHome };
