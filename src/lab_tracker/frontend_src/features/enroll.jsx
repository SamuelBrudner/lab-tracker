import * as React from "react";

import { apiRequest } from "../shared/api.js";
import { TOKEN_STORAGE_KEY } from "../shared/constants.js";

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

function EnrollPage({ replace, setFlash }) {
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
    apiRequest("/auth/devices/consume", {
      body: { offer_token: offer, label },
      method: "POST",
    })
      .then((payload) => {
        try {
          localStorage.setItem(TOKEN_STORAGE_KEY, payload.secret);
        } catch {
          // localStorage may be blocked; the user will see the failure on
          // the next request rather than here, and can pair again.
        }
        setPairedLabel(payload.label || label);
        setStatus("paired");
        setFlash(`Device paired as "${payload.label || label}".`);
        replace("/app/capture");
        if (typeof window !== "undefined") {
          // Force a reload so React picks up the new token on browsers that
          // cache the initial token snapshot in module-level closures.
          window.location.replace("/app/capture");
        }
      })
      .catch((err) => {
        setStatus("error");
        setError(err.message || "Pairing failed. Generate a new offer on your desktop.");
      });
  }, [offer, labelOverride, replace, setFlash]);

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
