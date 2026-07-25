import * as React from "react";

import { formatBytes, formatDate } from "../../shared/formatters.js";

const { useState } = React;
const OUTPUT_PAGE_SIZE = 100;

function SessionOutputsSection({ outputsState, onLoadOutputs }) {
  const [expanded, setExpanded] = useState(false);
  const offset = Number(outputsState.meta?.offset || 0);
  const total = Number(outputsState.meta?.total ?? outputsState.items.length);

  async function handleToggle() {
    const nextExpanded = !expanded;
    setExpanded(nextExpanded);
    if (nextExpanded && !outputsState.loaded && !outputsState.loading) {
      await onLoadOutputs(0);
    }
  }

  return (
    <div className="stack">
      <div className="item-head">
        <h3>Legacy Acquisition Outputs</h3>
        <span className="pill">{outputsState.loaded ? total : "not loaded"}</span>
      </div>
      <p className="subtle">
        Individual legacy outputs remain available in bounded pages. New high-cardinality runs use
        the collection summaries above.
      </p>
      <button
        type="button"
        className="btn-secondary"
        aria-expanded={expanded}
        disabled={outputsState.loading}
        onClick={handleToggle}
      >
        {expanded ? "Hide legacy outputs" : "Show legacy outputs"}
      </button>
      {expanded ? (
        <div className="stack">
          {outputsState.loading ? <p className="subtle">Loading up to 100 outputs...</p> : null}
          {outputsState.error ? <p className="flash error">{outputsState.error}</p> : null}
          {outputsState.loaded &&
          outputsState.items.length === 0 &&
          !outputsState.loading &&
          !outputsState.error ? (
            <p className="subtle">(no outputs)</p>
          ) : (
            <div className="stack">
              {outputsState.items.map((output) => (
                <div className="item" key={output.output_id}>
                  <div className="item-head">
                    <span className="mono">{output.file_path}</span>
                    <span className="subtle">{formatBytes(output.size_bytes)}</span>
                  </div>
                  <p className="mono">sha256: {output.checksum}</p>
                  <p className="subtle">{formatDate(output.created_at)}</p>
                </div>
              ))}
            </div>
          )}
          {total > OUTPUT_PAGE_SIZE ? (
            <div className="inline">
              <button
                type="button"
                className="btn-secondary"
                disabled={outputsState.loading || offset <= 0}
                onClick={() => onLoadOutputs(Math.max(0, offset - OUTPUT_PAGE_SIZE))}
              >
                Previous outputs
              </button>
              <span className="subtle">
                {offset + 1}-{Math.min(offset + outputsState.items.length, total)} of {total}
              </span>
              <button
                type="button"
                className="btn-secondary"
                disabled={
                  outputsState.loading || offset + outputsState.items.length >= total
                }
                onClick={() => onLoadOutputs(offset + OUTPUT_PAGE_SIZE)}
              >
                Next outputs
              </button>
            </div>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

export { SessionOutputsSection };
