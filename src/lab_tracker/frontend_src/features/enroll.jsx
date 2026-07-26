import * as React from "react";

import { auth as authGateway } from "../shared/gateways/index.js";

const { useEffect, useRef, useState } = React;

function readQueryParam(name) {
  try {
    const params = new URLSearchParams(window.location.search || "");
    return params.get(name) || "";
  } catch {
    return "";
  }
}

function suggestedLabel() {
  if (typeof navigator === "undefined") {
    return "Phone";
  }
  const ua = String(navigator.userAgent || "");
  if (/iPad/i.test(ua)) {
    return "iPad";
  }
  if (/iPhone/i.test(ua)) {
    return "iPhone";
  }
  if (/Android/i.test(ua)) {
    return "Android phone";
  }
  return "Phone";
}

function reloadAt(path) {
  if (typeof window !== "undefined") {
    window.location.replace(path);
  }
}

function EnrollPage({ persistTokenForReload, reload = reloadAt, replace, setFlash }) {
  const [offer] = useState(() => readQueryParam("offer"));
  const [labelOverride] = useState(() => readQueryParam("label"));
  const [status, setStatus] = useState("pairing"); // pairing | paired | error
  const [error, setError] = useState("");
  const [pairedLabel, setPairedLabel] = useState("");
  const attemptedRef = useRef(false);

  useEffect(() => {
    if (attemptedRef.current) {
      return;
    }
    attemptedRef.current = true;
    if (!offer) {
      setStatus("error");
      setError("This pairing link is missing the offer token. Generate a new one on your desktop.");
      return;
    }
    const label = (labelOverride && labelOverride.trim()) || suggestedLabel();
    authGateway
      .consumeDeviceEnrollment({ offer_token: offer, label })
      .then((payload) => {
        if (!persistTokenForReload(payload.secret)) {
          throw new Error(
            "Pairing succeeded, but this browser couldn’t save the new credential. " +
              "Enable browser storage and generate a new pairing code."
          );
        }
        setPairedLabel(payload.label || label);
        setStatus("paired");
        setFlash(`Device paired as "${payload.label || label}".`);
        const captureInstallPath = "/app/capture?install=1";
        replace(captureInstallPath);
        // Force a reload so React picks up the new token on browsers that cache
        // the initial token snapshot in module-level closures.
        reload(captureInstallPath);
      })
      .catch((err) => {
        setStatus("error");
        setError(err.message || "Pairing failed. Generate a new offer on your desktop.");
      });
  }, [offer, labelOverride, persistTokenForReload, reload, replace, setFlash]);

  return (
    <article className="card span-12 enroll-card">
      {status === "pairing" ? (
        <>
          <h2>Pairing this device…</h2>
          <p className="subtle">Saving credentials, then opening the capture screen.</p>
        </>
      ) : null}
      {status === "paired" ? (
        <>
          <h2>Paired as “{pairedLabel}”</h2>
          <p className="subtle">Opening the capture screen…</p>
        </>
      ) : null}
      {status === "error" ? (
        <>
          <h2>Pairing failed</h2>
          <p className="flash error">{error}</p>
          <p className="subtle">Generate a new pairing code on your desktop and scan it again.</p>
        </>
      ) : null}
    </article>
  );
}

export { EnrollPage };
