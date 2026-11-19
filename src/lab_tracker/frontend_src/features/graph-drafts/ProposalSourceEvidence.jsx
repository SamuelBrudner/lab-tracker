import * as React from "react";

const SOURCE_NAMES = {
  attachment_text: "saved attachment text",
  note_text: "saved note text",
  transcript: "saved transcript (derived)",
};

function ProposalSourceEvidence({ sources = [{ status: "unverified" }] }) {
  return (
    <span className="proposal-citation-evidence">
      <span className="subtle">Source evidence</span>
      {sources.map((source, index) => (
        <span className="proposal-citation-evidence" key={index}>
          {source.status === "ambiguous" ? (
            <span className="subtle">The citation does not identify a single saved source.</span>
          ) : source.status === "unverified" ? (
            <span className="subtle">The citation could not be verified against saved text.</span>
          ) : null}
          {source.artifact_id ? (
            <span>Saved attachment · {source.filename || "Attachment"}</span>
          ) : null}
          {source.excerpt ? (
            <>
              <span className="subtle">
                {source.status === "exact_excerpt" ? "Exact quote from" : "Excerpt from"} {SOURCE_NAMES[source.excerpt_kind]}
              </span>
              <span className="source-snippet" style={{ whiteSpace: "pre-wrap" }}>{source.excerpt}</span>
              {source.text_shortened ? <span className="subtle">Source text was shortened for review.</span> : null}
            </>
          ) : null}
          {source.note_id ? <a href={`/app/notes/${source.note_id}`}>Open source note</a> : null}
        </span>
      ))}
    </span>
  );
}

export { ProposalSourceEvidence };
