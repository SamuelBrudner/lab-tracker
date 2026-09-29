import * as React from "react";

import { useApiResource } from "../../hooks/useApiResource.js";

const { useState } = React;

/**
 * The signed-in person's private email capture address for the selected
 * project, when the server has email capture configured. Mail sent to it from
 * one of their registered addresses is staged as a note in that project. The
 * address works like a password, so it is only ever shown to the person.
 */
function EmailCapturePanel({ token, selectedProjectId }) {
  const [copyStatus, setCopyStatus] = useState("");
  const path =
    token && selectedProjectId
      ? `/projects/${encodeURIComponent(selectedProjectId)}/capture-address`
      : "";
  const { data, error, loading } = useApiResource(
    path,
    token,
    "Email capture address unavailable."
  );

  if (!selectedProjectId) {
    return null;
  }

  async function copyAddress() {
    let copied = false;
    if (data?.address && typeof navigator !== "undefined" && navigator.clipboard) {
      try {
        await navigator.clipboard.writeText(data.address);
        copied = true;
      } catch {
        copied = false;
      }
    }
    setCopyStatus(copied ? "Address copied." : "Copy failed; select the address instead.");
  }

  return (
    <section className="card-inset stack email-capture-panel" aria-labelledby="email-capture-title">
      <h3 id="email-capture-title">Email capture</h3>
      <p className="subtle">
        Forward results, instrument reports, or notes to this private address and they arrive as
        staged notes in this project. Only mail from your registered address is accepted, so keep
        the address to yourself.
      </p>
      {loading ? <span className="pill">Loading...</span> : null}
      {data?.address ? (
        <>
          <div className="mono email-capture-address">{data.address}</div>
          {data.accepted_senders?.length ? (
            <p className="subtle">Accepted from: {data.accepted_senders.join(", ")}</p>
          ) : null}
          <div className="inline">
            <button type="button" className="btn-secondary" onClick={copyAddress}>
              Copy address
            </button>
            {copyStatus ? <span className="subtle">{copyStatus}</span> : null}
          </div>
        </>
      ) : null}
      {error ? <p className="subtle">{error}</p> : null}
    </section>
  );
}

export { EmailCapturePanel };
