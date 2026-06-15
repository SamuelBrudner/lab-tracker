import * as React from "react";

import { apiListRequest, buildApiPath } from "../shared/api.js";
import { formatDate } from "../shared/formatters.js";

const { useCallback, useEffect, useMemo, useState } = React;

function draftProjectId(draft) {
  return draft?.project_id || draft?.context_packet?.project_id || "";
}

function draftSubmitterLabel(draft) {
  return (
    draft?.submitted_by_username ||
    draft?.created_by_username ||
    draft?.submitted_by ||
    draft?.created_by ||
    "Unassigned"
  );
}

function draftSubmittedAt(draft) {
  return draft?.submitted_at || draft?.updated_at || draft?.created_at || "";
}

function groupDraftsByProjectAndSubmitter(drafts, projects) {
  const projectNames = new Map(projects.map((project) => [project.project_id, project.name]));
  const groups = new Map();
  for (const draft of drafts) {
    const projectId = draftProjectId(draft);
    const projectKey = projectId || "unknown-project";
    const projectLabel = projectNames.get(projectId) || projectId || "Unknown project";
    const submitterKey = draft?.submitted_by || draft?.created_by || draftSubmitterLabel(draft);
    const submitterLabel = draftSubmitterLabel(draft);
    if (!groups.has(projectKey)) {
      groups.set(projectKey, {
        projectId,
        projectLabel,
        submitters: new Map(),
      });
    }
    const projectGroup = groups.get(projectKey);
    if (!projectGroup.submitters.has(submitterKey)) {
      projectGroup.submitters.set(submitterKey, {
        drafts: [],
        submitterLabel,
      });
    }
    projectGroup.submitters.get(submitterKey).drafts.push(draft);
  }
  return Array.from(groups.values())
    .map((projectGroup) => ({
      ...projectGroup,
      submitters: Array.from(projectGroup.submitters.values())
        .map((submitterGroup) => ({
          ...submitterGroup,
          drafts: submitterGroup.drafts.sort((left, right) =>
            draftSubmittedAt(right).localeCompare(draftSubmittedAt(left))
          ),
        }))
        .sort((left, right) => left.submitterLabel.localeCompare(right.submitterLabel)),
    }))
    .sort((left, right) => left.projectLabel.localeCompare(right.projectLabel));
}

function operationCountLabel(draft) {
  const count = (draft?.operations || []).length;
  return count === 1 ? "1 op" : `${count} ops`;
}

function ReviewQueuePage({ token, projects, navigate, setFlash }) {
  const [drafts, setDrafts] = useState([]);
  const [loading, setLoading] = useState(false);
  const [projectFilter, setProjectFilter] = useState("");

  const groupedDrafts = useMemo(
    () => groupDraftsByProjectAndSubmitter(drafts, projects),
    [drafts, projects]
  );

  const loadDrafts = useCallback(async () => {
    setLoading(true);
    try {
      const path = buildApiPath("/graph-drafts", {
        status: "submitted",
        project_id: projectFilter,
        limit: 100,
      });
      const { data } = await apiListRequest(path, { token });
      setDrafts(data || []);
    } catch (err) {
      setFlash("", err.message || "Failed to load review queue.");
    } finally {
      setLoading(false);
    }
  }, [projectFilter, setFlash, token]);

  useEffect(() => {
    loadDrafts();
  }, [loadDrafts]);

  return (
    <article className="card span-12">
      <div className="item-head">
        <h2>Lab Review Queue</h2>
        {loading ? <span className="pill">Loading...</span> : null}
      </div>

      <div className="review-layout">
        <section className="review-pane">
          <label>
            Review scope
            <select
              value={projectFilter}
              onChange={(event) => setProjectFilter(event.target.value)}
            >
              <option value="">All accessible projects</option>
              {projects.map((project) => (
                <option key={project.project_id} value={project.project_id}>
                  {project.name}
                </option>
              ))}
            </select>
          </label>
          <div className="inline detail-actions">
            <button type="button" className="btn-secondary" onClick={loadDrafts}>
              Refresh
            </button>
          </div>
        </section>

        <section className="review-pane">
          <div className="item-head">
            <h3>Pending Review</h3>
            <span className="pill">{drafts.length} drafts</span>
          </div>
          <div className="stack">
            {groupedDrafts.length === 0 ? (
              <p className="subtle">No submitted graph drafts.</p>
            ) : (
              groupedDrafts.map((projectGroup) => (
                <section className="stack" key={projectGroup.projectId || projectGroup.projectLabel}>
                  <div className="item-head">
                    <h3>{projectGroup.projectLabel}</h3>
                    <span className="pill">
                      {projectGroup.submitters.reduce(
                        (total, submitterGroup) => total + submitterGroup.drafts.length,
                        0
                      )}{" "}
                      drafts
                    </span>
                  </div>
                  {projectGroup.submitters.map((submitterGroup) => (
                    <article className="item" key={submitterGroup.submitterLabel}>
                      <div className="item-head">
                        <strong>{submitterGroup.submitterLabel}</strong>
                        <span className="pill">{submitterGroup.drafts.length} drafts</span>
                      </div>
                      <div className="stack">
                        {submitterGroup.drafts.map((draft) => (
                          <article className="item graph-operation-card" key={draft.change_set_id}>
                            <div className="item-head">
                              <strong>{draft.summary || draft.source_filename || "Graph draft"}</strong>
                              <span className="pill review-pending">pending review</span>
                            </div>
                            <div className="inline">
                              <span className="pill">{formatDate(draftSubmittedAt(draft))}</span>
                              <span className="pill">{operationCountLabel(draft)}</span>
                              {draft.draft_mode ? <span className="pill">{draft.draft_mode}</span> : null}
                              {draft.model ? <span className="pill">{draft.model}</span> : null}
                            </div>
                            {draft.source_filename ? <p className="subtle">{draft.source_filename}</p> : null}
                            <button
                              type="button"
                              className="btn-primary"
                              onClick={() => navigate(`/app/graph-drafts/${draft.change_set_id}`)}
                            >
                              Review draft
                            </button>
                          </article>
                        ))}
                      </div>
                    </article>
                  ))}
                </section>
              ))
            )}
          </div>
        </section>
      </div>
    </article>
  );
}

export { ReviewQueuePage };
