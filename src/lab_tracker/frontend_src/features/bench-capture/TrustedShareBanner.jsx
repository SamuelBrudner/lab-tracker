import * as React from "react";

import { SHARE_TRUST_HOURS, formatTrustRemaining } from "./trusted-share.js";

/** Visible while a trusted share window is open, with its time left and a Stop. */
function TrustedShareBanner({ trust, checkedAt, onStop }) {
  if (!trust) {
    return null;
  }
  return (
    <div className="flash trusted-share-banner" role="status">
      <span>
        Shares from this phone&apos;s share sheet go straight into{" "}
        <strong>{trust.sessionLabel || "the trusted session"}</strong> without review for{" "}
        {formatTrustRemaining(trust.expiresAt - checkedAt)} more.
      </span>{" "}
      <button type="button" className="btn-secondary" onClick={onStop}>
        Stop
      </button>
    </div>
  );
}

/**
 * "Trust shares into <session> for 1h / 2h / 4h": offered next to the share
 * review so a person sharing a batch confirms once instead of per item.
 */
function TrustShareChoices({ sessionLabel, disabled = false, onTrust }) {
  if (!sessionLabel || !onTrust) {
    return null;
  }
  return (
    <div className="stack trust-share-choices">
      <p className="subtle">
        Sharing several items? Trust shares into <strong>{sessionLabel}</strong> for a while and
        they skip this step, on this device only.
      </p>
      <div className="inline" role="group" aria-label={`Trust shares into ${sessionLabel}`}>
        <span>Trust shares into this session for</span>
        {SHARE_TRUST_HOURS.map((hours) => (
          <button
            key={hours}
            type="button"
            className="btn-secondary"
            disabled={disabled}
            aria-label={`Trust shares into ${sessionLabel} for ${hours} ${
              hours === 1 ? "hour" : "hours"
            }`}
            onClick={() => onTrust(hours)}
          >
            {hours}h
          </button>
        ))}
      </div>
    </div>
  );
}

export { TrustShareChoices, TrustedShareBanner };
