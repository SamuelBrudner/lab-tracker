import * as React from "react";

import { apiListRequest, apiRequest } from "../shared/api.js";
import { formatDate } from "../shared/formatters.js";
import {
  disablePushNotifications,
  enablePushNotifications,
  getPushNotificationState,
} from "../shared/push-notifications.js";
import { BenchTagsCard } from "./tags.jsx";

const { useCallback, useEffect, useState } = React;

function DevicesPage({ token, canWrite, navigate, setFlash }) {
  const [devices, setDevices] = useState([]);
  const [loading, setLoading] = useState(true);
  const [creating, setCreating] = useState(false);
  const [pendingOffer, setPendingOffer] = useState(null);
  const [revokingId, setRevokingId] = useState("");
  const [pushState, setPushState] = useState(null);
  const [pushBusy, setPushBusy] = useState(false);
  const isPairedDevice = String(token || "").startsWith("ldev_");

  const refresh = useCallback(async () => {
    if (isPairedDevice) {
      setDevices([]);
      setLoading(false);
      return;
    }
    setLoading(true);
    try {
      const page = await apiListRequest("/auth/devices", { token });
      setDevices(page.data || []);
    } catch (err) {
      setFlash("", err.message || "Failed to load paired devices.");
    } finally {
      setLoading(false);
    }
  }, [isPairedDevice, setFlash, token]);

  const refreshPushState = useCallback(async () => {
    try {
      setPushState(await getPushNotificationState({ token }));
    } catch (err) {
      setPushState(null);
      setFlash("", err.message || "Failed to load review notification settings.");
    }
  }, [setFlash, token]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  useEffect(() => {
    refreshPushState();
  }, [refreshPushState]);

  async function handleEnablePush() {
    setPushBusy(true);
    setFlash("", "");
    try {
      await enablePushNotifications({ token });
      await refreshPushState();
      setFlash("Review notifications enabled on this device.");
    } catch (err) {
      setFlash("", err.message || "Failed to enable review notifications.");
    } finally {
      setPushBusy(false);
    }
  }

  async function handleDisablePush() {
    setPushBusy(true);
    setFlash("", "");
    try {
      await disablePushNotifications({ token });
      await refreshPushState();
      setFlash("Review notifications disabled on this device.");
    } catch (err) {
      setFlash("", err.message || "Failed to disable review notifications.");
    } finally {
      setPushBusy(false);
    }
  }

  async function handleCreate() {
    setCreating(true);
    setFlash("", "");
    try {
      const offer = await apiRequest("/auth/devices/enrollment", {
        body: {},
        method: "POST",
        token,
      });
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
      await apiRequest(`/auth/devices/${deviceTokenId}`, {
        method: "DELETE",
        token,
      });
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
          <h2>Devices &amp; notifications</h2>
          <p className="subtle">
            Enable a generic alert when your daily review is ready. Notification
            text never includes project names or scientific content.
          </p>
        </div>
        <button type="button" className="btn-secondary" onClick={() => navigate("/app")}>
          Workspace
        </button>
      </div>

      <div className="card-inset form">
        <h3>Review notifications on this device</h3>
        {!pushState ? (
          <p className="subtle">Loading notification settings…</p>
        ) : !pushState.configured ? (
          <p className="subtle">Web Push is not configured on this Lab Tracker instance.</p>
        ) : !pushState.secure ? (
          <p className="subtle">Review notifications require HTTPS or localhost.</p>
        ) : !pushState.supported ? (
          <p className="subtle">This browser does not support Web Push notifications.</p>
        ) : pushState.permission === "denied" ? (
          <p className="subtle">
            Notifications are blocked in browser settings for this site.
          </p>
        ) : pushState.subscribed ? (
          <button
            type="button"
            className="btn-secondary"
            disabled={pushBusy}
            onClick={handleDisablePush}
          >
            {pushBusy ? "Disabling…" : "Disable review notifications"}
          </button>
        ) : (
          <button
            type="button"
            className="btn-primary"
            disabled={pushBusy}
            onClick={handleEnablePush}
          >
            {pushBusy ? "Enabling…" : "Enable review notifications"}
          </button>
        )}
      </div>

      {!isPairedDevice ? (
        <div className="form">
          <p className="subtle">
            Pair a phone once on this desktop; it can capture without re-entering
            your password. Revoke any device any time.
          </p>
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
      ) : null}

      {isPairedDevice ? null : loading ? (
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

      {!isPairedDevice ? (
        <BenchTagsCard token={token} canWrite={canWrite} setFlash={setFlash} />
      ) : null}
    </article>
  );
}

export { DevicesPage };
