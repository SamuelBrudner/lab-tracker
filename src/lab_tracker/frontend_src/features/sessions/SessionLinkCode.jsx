import * as React from "react";

const { useState } = React;

// The watcher and `lt watch add --session` claim a session from a folder or
// file name only through this explicit prefix, so the app shows and copies the
// code in that form. API paths and payloads keep the bare code.
const SESSION_LINK_CODE_PREFIX = "LT-";
const COPIED_LABEL = "Copied";
const COPY_FAILED_LABEL = "Copy failed; select the code instead";

function prefixedLinkCode(linkCode) {
  return `${SESSION_LINK_CODE_PREFIX}${linkCode}`;
}

async function writeClipboard(text) {
  if (typeof navigator === "undefined" || !navigator.clipboard) {
    return false;
  }
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    return false;
  }
}

function SessionLinkCode({ linkCode }) {
  const [copyStatus, setCopyStatus] = useState("");
  if (!linkCode) {
    return null;
  }
  const code = prefixedLinkCode(linkCode);

  async function copyCode() {
    setCopyStatus((await writeClipboard(code)) ? COPIED_LABEL : COPY_FAILED_LABEL);
  }

  return (
    <>
      <div className="subtle">Link code</div>
      <div className="inline">
        <span className="mono">{code}</span>
        <button
          type="button"
          className="btn-secondary"
          aria-label="Copy link code"
          onClick={copyCode}
        >
          Copy
        </button>
        {copyStatus ? (
          <span className="subtle" role="status">
            {copyStatus}
          </span>
        ) : null}
      </div>
      <div className="subtle">
        Put it in a watched folder or file name, e.g. <span className="mono">session001_{code}</span>,
        to file what is saved there into this session.
      </div>
    </>
  );
}

export { SESSION_LINK_CODE_PREFIX, SessionLinkCode, prefixedLinkCode };
