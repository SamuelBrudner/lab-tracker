import * as React from "react";

import { auth as authGateway } from "../shared/gateways/index.js";
import { formatDate } from "../shared/formatters.js";

const { useCallback, useEffect, useState } = React;

function DevicesPage({ token, canWrite, navigate, setFlash }) {
  const [devices, setDevices] = useState([]);
  const [loading, setLoading] = useState(true);
  const [creating, setCreating] = useState(false);
  const [pendingOffer, setPendingOffer] = useState(null);
  const [revokingId, setRevokingId] = useState("");

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const page = await authGateway.listDevices({ token });
      setDevices(page.data);
    } catch (err) {
      setFlash("", err.message || "Failed to load paired devices.");
    } finally {
      setLoading(false);
    }
  }, [setFlash, token]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  async function handleCreate() {
    setCreating(true);
    setFlash("", "");
    try {
      const offer = await authGateway.createDeviceEnrollment({}, { token });
      setPendingOffer(offer);
    } catch (err) {
      setFlash("", err.message || "Failed to create enrollment offer.");
    } finally {
      setCreating(false);
    }
  }

  async function handleRevoke(deviceTokenId) {
    setRevokingId(deviceTokenId);
    setFlash("", "");
    try {
      await authGateway.revokeDevice(deviceTokenId, { token });
      setFlash("Device revoked.");
      await refresh();
    } catch (err) {
      setFlash("", err.message || "Failed to revoke device.");
    } finally {
      setRevokingId("");
    }
  }

  async function copyEnrollmentUrl(url) {
    if (typeof navigator !== "undefined" && navigator.clipboard) {
      try {
        await navigator.clipboard.writeText(url);
        setFlash("Enrollment URL copied. Open it on the phone to finish pairing.");
        return;
      } catch {
        // fall through to selection-based fallback below
      }
    }
    setFlash("Copy the URL below and open it on the phone.");
  }

  return (
    <article className="card span-12">
      <div className="item-head">
        <div>
          <h2>Paired devices</h2>
          <p className="subtle">
            Pair a phone once on this desktop; it will capture into Lab Tracker
            without re-entering your password. Revoke any device any time.
          </p>
        </div>
        <button type="button" className="btn-secondary" onClick={() => navigate("/app")}>
          Workspace
        </button>
      </div>

      <div className="form">
        <button
          type="button"
          className="btn-primary"
          disabled={!canWrite || creating}
          onClick={handleCreate}
        >
          {creating ? "Creating offer…" : "Add a new device"}
        </button>

        {pendingOffer ? (
          <div className="card-inset enrollment-offer">
            <h3>Scan with the phone's camera</h3>
            <p className="subtle">
              Point the phone's camera at this code, tap the pop-up, and the
              device pairs itself — no typing. The code expires{" "}
              {pendingOffer.expires_at ? formatDate(pendingOffer.expires_at) : "shortly"}.
            </p>
            {pendingOffer.enrollment_qr_svg ? (
              <div
                className="enrollment-qr"
                aria-label="Pairing QR code"
                /* segno returns an inline <svg>; the document already controls
                   what URL it encodes, so trusted insertion is fine. */
                dangerouslySetInnerHTML={{ __html: pendingOffer.enrollment_qr_svg }}
              />
            ) : null}
            <details className="enrollment-fallback">
              <summary>Can't scan? Open this URL on the phone</summary>
              <div className="enrollment-url">
                <code>{pendingOffer.enrollment_url}</code>
                <button
                  type="button"
                  className="btn-secondary"
                  onClick={() => copyEnrollmentUrl(pendingOffer.enrollment_url)}
                >
                  Copy URL
                </button>
              </div>
            </details>
            <button
              type="button"
              className="btn-link"
              onClick={() => setPendingOffer(null)}
            >
              Dismiss
            </button>
          </div>
        ) : null}
      </div>

      {loading ? (
        <p className="subtle">Loading paired devices…</p>
      ) : devices.length === 0 ? (
        <p className="subtle">No devices paired yet.</p>
      ) : (
        <ul className="list-clean">
          {devices.map((device) => (
            <li key={device.device_token_id} className="row-between">
              <div>
                <strong>{device.label}</strong>
                <div className="subtle">
                  Paired {formatDate(device.created_at)}
                  {device.last_used_at
                    ? ` · last used ${formatDate(device.last_used_at)}`
                    : " · not yet used"}
                  {device.revoked_at ? " · revoked" : ""}
                </div>
              </div>
              {!device.revoked_at ? (
                <button
                  type="button"
                  className="btn-danger"
                  disabled={!canWrite || revokingId === device.device_token_id}
                  onClick={() => handleRevoke(device.device_token_id)}
                >
                  {revokingId === device.device_token_id ? "Revoking…" : "Revoke"}
                </button>
              ) : null}
            </li>
          ))}
        </ul>
      )}
    </article>
  );
}

export { DevicesPage };
