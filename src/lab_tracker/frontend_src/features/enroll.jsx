import * as React from "react";

import { apiRequest } from "../shared/api.js";
import { TOKEN_STORAGE_KEY } from "../shared/constants.js";

const { useEffect, useState } = React;

function readOfferFromLocation() {
  try {
    const params = new URLSearchParams(window.location.search || "");
    return params.get("offer") || "";
  } catch {
    return "";
  }
}

function suggestedLabel() {
  if (typeof navigator === "undefined") {
    return "Phone";
  }
  const ua = String(navigator.userAgent || "");
  if (/iPhone/i.test(ua)) {
    return "iPhone";
  }
  if (/iPad/i.test(ua)) {
    return "iPad";
  }
  if (/Android/i.test(ua)) {
    return "Android phone";
  }
  return "Phone";
}

function EnrollPage({ replace, setFlash }) {
  const [offer] = useState(() => readOfferFromLocation());
  const [label, setLabel] = useState(() => suggestedLabel());
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    if (!offer) {
      setError("This pairing link is missing the offer token. Generate a new one on your desktop.");
    }
  }, [offer]);

  async function handleSubmit(event) {
    event.preventDefault();
    if (!offer) {
      return;
    }
    if (!label.trim()) {
      setError("Choose a name for this device.");
      return;
    }
    setSubmitting(true);
    setError("");
    setFlash("", "");
    try {
      const payload = await apiRequest("/auth/devices/consume", {
        body: { offer_token: offer, label: label.trim() },
        method: "POST",
      });
      try {
        localStorage.setItem(TOKEN_STORAGE_KEY, payload.secret);
      } catch {
        // localStorage may be blocked; the user will see the failure on
        // the next request rather than here, and can pair again.
      }
      setFlash(`Device paired as "${payload.label}".`);
      replace("/app/capture");
      // The capture route picks up the new token through useAuthSession.
      // Force a reload so the React tree re-initializes with it on
      // browsers that aggressively cache the initial token snapshot.
      if (typeof window !== "undefined") {
        window.location.replace("/app/capture");
      }
    } catch (err) {
      setError(err.message || "Pairing failed. Generate a new offer on your desktop.");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <article className="card span-12">
      <div className="item-head">
        <div>
          <h2>Pair this device</h2>
          <p className="subtle">
            Give this phone a name and tap Pair. After this, captures save to
            Lab Tracker without a password.
          </p>
        </div>
      </div>
      <form className="form" onSubmit={handleSubmit}>
        <label>
          Device name
          <input
            type="text"
            value={label}
            disabled={submitting || !offer}
            onChange={(event) => setLabel(event.target.value)}
            maxLength={150}
            autoFocus
          />
        </label>
        {error ? <p className="flash error">{error}</p> : null}
        <button
          type="submit"
          className="btn-primary"
          disabled={submitting || !offer || !label.trim()}
        >
          {submitting ? "Pairing…" : "Pair this device"}
        </button>
      </form>
    </article>
  );
}

export { EnrollPage };
