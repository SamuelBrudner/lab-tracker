import * as React from "react";

import { REJECT_REASONS } from "./review-reasons.js";

// One chip per structured reason, each prefixed by the digit that picks it
// from the keyboard; the Cancel chip (or Escape) backs out without a request.
function ReasonChips({
  reasons = REJECT_REASONS,
  disabled = false,
  label = "Reason",
  onChoose,
  onCancel,
}) {
  return (
    <div className="review-reason-chips" role="group" aria-label={label}>
      {reasons.map((reason) => (
        <button
          type="button"
          className="review-reason-chip"
          disabled={disabled}
          key={reason.value}
          onClick={() => onChoose(reason.value)}
        >
          <kbd>{reason.key}</kbd> {reason.label}
        </button>
      ))}
      <button type="button" className="btn-link" disabled={disabled} onClick={onCancel}>
        Cancel
      </button>
    </div>
  );
}

export { ReasonChips };
