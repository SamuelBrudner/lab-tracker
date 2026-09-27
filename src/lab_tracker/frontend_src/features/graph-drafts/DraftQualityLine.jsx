import * as React from "react";

import { apiRequest } from "../../shared/api.js";
import { draftQualitySummaryLine } from "./draft-quality.js";

// One advisory sentence from the project's draft-quality ledger. It is a
// read of the review record, so a failed fetch renders nothing and never
// interrupts the review with a flash.
function DraftQualityLine({ projectId, token, changeSet }) {
  const [ledger, setLedger] = React.useState(null);
  React.useEffect(() => {
    let canceled = false;
    setLedger(null);
    if (!projectId) {
      return () => {
        canceled = true;
      };
    }
    apiRequest(`/projects/${projectId}/draft-quality`, { token })
      .then((data) => {
        if (!canceled) {
          setLedger(data);
        }
      })
      .catch(() => {
        if (!canceled) {
          setLedger(null);
        }
      });
    return () => {
      canceled = true;
    };
  }, [projectId, token]);

  const line = ledger ? draftQualitySummaryLine(ledger, changeSet) : "";
  if (!line) {
    return null;
  }
  return (
    <p className="review-quality subtle" role="status">
      {line}
    </p>
  );
}

export { DraftQualityLine };
