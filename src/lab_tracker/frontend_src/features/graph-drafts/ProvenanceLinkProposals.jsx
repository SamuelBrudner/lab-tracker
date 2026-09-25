import * as React from "react";

import { apiListRequest, buildApiPath } from "../../shared/api.js";

const PROPOSAL_PAGE_SIZE = 50;

function entityLabel(ref) {
  return `${ref?.entity_type || "?"} ${ref?.entity_id || ""}`.trim();
}

// The project's proposed provenance links (the content-hash detector's
// "these two captures share bytes" proposals), each with an Accept / Reject
// decision. Only a person turns a proposal into lineage, so the section
// disappears once the list is empty.
function ProvenanceLinkProposals({ projectId, token, canWrite, onDecide }) {
  const [links, setLinks] = React.useState([]);
  const [pendingId, setPendingId] = React.useState("");

  React.useEffect(() => {
    let canceled = false;
    setLinks([]);
    if (!projectId) {
      return () => {
        canceled = true;
      };
    }
    apiListRequest(
      buildApiPath("/provenance-links", {
        limit: PROPOSAL_PAGE_SIZE,
        project_id: projectId,
        status: "proposed",
      }),
      { token }
    )
      .then(({ data }) => {
        if (!canceled) {
          setLinks(data || []);
        }
      })
      .catch(() => {
        if (!canceled) {
          setLinks([]);
        }
      });
    return () => {
      canceled = true;
    };
  }, [projectId, token]);

  if (links.length === 0) {
    return null;
  }

  async function decide(link, status) {
    setPendingId(link.link_id);
    try {
      const decided = await onDecide(link.link_id, status);
      if (decided) {
        setLinks((current) => current.filter((item) => item.link_id !== link.link_id));
      }
    } finally {
      setPendingId("");
    }
  }

  return (
    <section className="provenance-proposals" aria-label="Proposed provenance links">
      <h3>Proposed provenance links</h3>
      <ul className="compact-list">
        {links.map((link) => (
          <li className="provenance-proposal" key={link.link_id}>
            <span>
              <span className="mono">{entityLabel(link.source)}</span>
              {" → "}
              <span className="mono">{entityLabel(link.target)}</span>
              <span className="subtle">
                {" "}
                · {link.relation} · {link.basis}
                {link.content_hash ? (
                  <>
                    {" "}
                    · <span className="mono">{link.content_hash}</span>
                  </>
                ) : null}
              </span>
            </span>
            <span className="inline">
              <button
                type="button"
                className="btn-primary"
                disabled={!canWrite || pendingId === link.link_id}
                onClick={() => decide(link, "accepted")}
              >
                Accept
              </button>
              <button
                type="button"
                className="btn-danger"
                disabled={!canWrite || pendingId === link.link_id}
                onClick={() => decide(link, "rejected")}
              >
                Reject
              </button>
            </span>
          </li>
        ))}
      </ul>
    </section>
  );
}

export { ProvenanceLinkProposals };
