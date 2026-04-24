import * as React from "react";

import { formatBytes, formatDate } from "../../shared/formatters.js";

function SessionOutputsSection({ outputsState }) {
  return (
    <div className="stack">
      <div className="item-head">
        <h3>Acquisition Outputs</h3>
        <span className="pill">{outputsState.items.length}</span>
      </div>
      {outputsState.loading ? <p className="subtle">Loading outputs...</p> : null}
      {outputsState.error ? <p className="flash error">{outputsState.error}</p> : null}
      {outputsState.items.length === 0 && !outputsState.loading ? (
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
    </div>
  );
}

export { SessionOutputsSection };
