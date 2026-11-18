import * as React from "react";
import { apiRequest } from "../shared/api.js";

const endpoint = "/auth/review-mailbox";

export function ReviewMailbox({ token }) {
  const [expanded, setExpanded] = React.useState(false);
  const [status, setStatus] = React.useState(null);
  const [loginEmail, setLoginEmail] = React.useState("");
  const [code, setCode] = React.useState("");
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState("");

  React.useEffect(() => {
    let current = true;
    apiRequest(endpoint, { token }).then((result) => {
      if (current) {
        setStatus(result);
        setLoginEmail(result.pending_email || result.login_email || "");
      }
    }).catch(() => {
      if (current) setStatus(null);
    });
    return () => { current = false; };
  }, [token]);

  async function request(path, options) {
    setBusy(true);
    setError("");
    try {
      const result = await apiRequest(endpoint + path, { token, ...options });
      setStatus(result);
      setCode("");
      setLoginEmail(result.pending_email || result.login_email || "");
    } catch (err) {
      setError(err.message || "Could not update your review mailbox.");
    } finally {
      setBusy(false);
    }
  }

  if (!status || (!status.available && !status.enabled)) return null;

  return (
    <section className="stack">
      <button
        type="button"
        className="btn-secondary"
        aria-expanded={expanded}
        disabled={busy}
        onClick={() => setExpanded(!expanded)}
      >
        Review proposals by email
      </button>
      {expanded && (
        <div className="stack">
          {error && <p role="alert">{error}</p>}
          {busy && <p role="status">Updating review email…</p>}
          {status?.enabled ? (
            <>
              <p>
                {status.available
                  ? `Proposal review is enabled for ${status.login_email}.`
                  : `You enrolled ${status.login_email}. Delivery is currently unavailable.`}
              </p>
              <button
                type="button"
                className="btn-secondary"
                disabled={busy}
                onClick={() => request("", { method: "DELETE" })}
              >
                Turn off proposal review by email
              </button>
            </>
          ) : (
            <>
              <p>
                Receive assigned proposal details in your inbox and reply to
                approve or correct them. This applies across your projects.
              </p>
              {status.login_email ? (
                <p>Login email: {status.login_email}</p>
              ) : (
                <label>
                  New login email
                  <input
                    type="email"
                    value={loginEmail}
                    maxLength={150}
                    disabled={busy}
                    autoComplete="email"
                    onChange={(event) => setLoginEmail(event.target.value)}
                  />
                  After verification, sign in with this email and your current password.
                </label>
              )}
              <button
                type="button"
                className="btn-secondary"
                disabled={busy || !(status.login_email || loginEmail.trim())}
                onClick={() => request("/challenge", {
                  method: "POST",
                  body: status.login_email ? {} : { login_email: loginEmail.trim() },
                })}
              >
                {status.pending_email ? "Send another code" : "Send verification code"}
              </button>
              {status.pending_email && (
                <>
                  <p>Check {status.pending_email} for your 15-minute verification code.</p>
                  <label>
                    Verification code
                    <input
                      value={code}
                      autoComplete="one-time-code"
                      disabled={busy}
                      onChange={(event) => setCode(event.target.value)}
                    />
                  </label>
                  <button
                    type="button"
                    className="btn-primary"
                    disabled={busy || !code.trim()}
                    onClick={() => request("/confirm", {
                      method: "POST", body: { code: code.trim() },
                    })}
                  >
                    Verify and enable email review
                  </button>
                </>
              )}
            </>
          )}
        </div>
      )}
    </section>
  );
}
