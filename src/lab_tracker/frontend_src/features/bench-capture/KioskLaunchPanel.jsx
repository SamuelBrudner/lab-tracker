import * as React from "react";

/** The capture route that opens the bench kiosk, optionally on one project and session. */
function kioskRoute({ projectId = "", sessionId = "" } = {}) {
  const params = new URLSearchParams({ kiosk: "1" });
  if (projectId) {
    params.set("project_id", projectId);
  }
  if (sessionId) {
    params.set("session_id", sessionId);
  }
  return `/app/capture?${params.toString()}`;
}

/**
 * Devices-page entry point for the bench kiosk: a chrome-free scan station for
 * a shared bench computer with a USB barcode scanner. Opening it writes
 * nothing; each scan the person makes there is staged like any other capture.
 */
function KioskLaunchPanel({ navigate, selectedProjectId }) {
  return (
    <section className="card-inset stack kiosk-launch-panel" aria-labelledby="kiosk-launch-title">
      <h3 id="kiosk-launch-title">Bench kiosk</h3>
      <p className="subtle">
        Turn a shared bench computer with a USB barcode scanner into a scan station. Every scan
        becomes a staged note with its time, in the session you choose, and scans made while the
        server is unreachable wait on the computer until it is back. Open it on the bench
        computer and leave it running; to start it on one session, use Open bench kiosk on that
        session&apos;s page.
      </p>
      <div className="inline">
        <button
          type="button"
          className="btn-secondary"
          onClick={() => navigate(kioskRoute({ projectId: selectedProjectId }))}
        >
          Open bench kiosk
        </button>
      </div>
    </section>
  );
}

export { KioskLaunchPanel, kioskRoute };
