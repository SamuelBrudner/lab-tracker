import * as React from "react";

import { useApiResource } from "../../hooks/useApiResource.js";

function captureRoute(session) {
  return `/app/capture?project_id=${encodeURIComponent(
    session.project_id
  )}&session_id=${encodeURIComponent(session.session_id)}`;
}

// "Capture into this session": a server-rendered QR the bench phone scans so
// every capture it makes lands with this session preselected, plus the same
// link for the device you are already on. Read-only; nothing here writes.
function SessionCaptureLinkSection({ token, session, navigate }) {
  const { data: link, error, loading } = useApiResource(
    token && session?.session_id ? `/sessions/${session.session_id}/capture-link` : "",
    token,
    "Capture link unavailable."
  );

  if (!session) {
    return null;
  }

  return (
    <section className="stack session-capture-link" aria-labelledby="session-capture-link-title">
      <div className="item-head">
        <h3 id="session-capture-link-title">Capture into this session</h3>
        {loading ? <span className="pill">Loading...</span> : null}
      </div>
      <p className="subtle">
        Scan with a paired phone, or capture from here. Notes, photos, and voice memos then
        arrive already linked to this session.
      </p>
      {link?.capture_qr_svg ? (
        <div
          aria-label="Session capture QR code"
          className="enrollment-qr session-capture-qr"
          role="img"
          // Server-generated SVG markup for a phone scanner; identical treatment
          // to the device-pairing QR.
          dangerouslySetInnerHTML={{ __html: link.capture_qr_svg }}
        />
      ) : null}
      {link?.capture_url ? <div className="mono session-capture-url">{link.capture_url}</div> : null}
      {error ? <p className="subtle">{error}</p> : null}
      <div className="inline">
        <button
          type="button"
          className="btn-primary"
          onClick={() => navigate(captureRoute(session))}
        >
          Capture on this device
        </button>
      </div>
    </section>
  );
}

export { SessionCaptureLinkSection, captureRoute };
