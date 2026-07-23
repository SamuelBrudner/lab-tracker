import * as React from "react";

// Presentational SVG glyph for a capture affordance (voice/bundle/text/photo).
function CaptureIcon({ kind }) {
  if (kind === "voice") {
    return (
      <svg aria-hidden="true" className="capture-icon" viewBox="0 0 24 24">
        <rect height="11" rx="4" width="7" x="8.5" y="3.5" />
        <path d="M5.5 11.5v1.2a6.5 6.5 0 0 0 13 0v-1.2" />
        <path d="M12 19.2v2.3" />
        <path d="M8.4 21.5h7.2" />
      </svg>
    );
  }
  if (kind === "bundle") {
    return (
      <svg aria-hidden="true" className="capture-icon" viewBox="0 0 24 24">
        <rect height="9.5" rx="2" width="11" x="3.5" y="6.5" />
        <path d="M6.5 6.5l1.2-2h2.8l1.2 2" />
        <circle cx="9" cy="11.4" r="2.4" />
        <rect height="8.5" rx="3" width="5.5" x="16" y="5" />
        <path d="M14.8 14.5a4 4 0 0 0 8 0" />
        <path d="M18.8 18.5v2" />
      </svg>
    );
  }
  if (kind === "text") {
    return (
      <svg aria-hidden="true" className="capture-icon" viewBox="0 0 24 24">
        <path d="M5 5.5h14" />
        <path d="M12 5.5v13" />
        <path d="M8 18.5h8" />
        <path d="M5.5 9h5" />
        <path d="M13.5 9h5" />
      </svg>
    );
  }
  return (
    <svg aria-hidden="true" className="capture-icon" viewBox="0 0 24 24">
      <rect height="11.5" rx="2.2" width="17" x="3.5" y="7" />
      <path d="M7.2 7l1.5-2.3h6.6L16.8 7" />
      <circle cx="12" cy="12.8" r="3.2" />
      <path d="M17.3 9.8h.1" />
    </svg>
  );
}

export { CaptureIcon };
