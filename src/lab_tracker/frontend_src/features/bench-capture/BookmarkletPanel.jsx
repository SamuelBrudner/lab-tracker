import * as React from "react";

import { resolveAppPath } from "../../shared/routing.jsx";
import { bookmarkletSource } from "./bookmarklet.js";

const { useEffect, useRef, useState } = React;

function captureAppUrl() {
  return `${window.location.origin}${resolveAppPath("/app/capture")}`;
}

/**
 * A draggable "Save to Lab Tracker" bookmarklet for desktop browsers. It opens
 * the capture page in a new window with the current page's title, address, and
 * selected text prefilled; the person reads it there and presses send. It
 * stores a pointer (title, URL, a bounded excerpt), never a copy of the page.
 */
function BookmarkletPanel() {
  const linkRef = useRef(null);
  const [hint, setHint] = useState("");
  const [copyStatus, setCopyStatus] = useState("");
  const source = bookmarkletSource(captureAppUrl());

  useEffect(() => {
    // React refuses javascript: URLs in href, so the bookmark target is set on
    // the DOM node directly. It is this app's own fixed script.
    linkRef.current?.setAttribute("href", source);
  }, [source]);

  async function copySource() {
    let copied = false;
    if (typeof navigator !== "undefined" && navigator.clipboard) {
      try {
        await navigator.clipboard.writeText(source);
        copied = true;
      } catch {
        copied = false;
      }
    }
    setCopyStatus(copied ? "Bookmarklet copied." : "Copy failed; select the text instead.");
  }

  return (
    <section className="card-inset stack bookmarklet-panel" aria-labelledby="bookmarklet-title">
      <h3 id="bookmarklet-title">Desktop bookmarklet</h3>
      <p className="subtle">
        Drag the button to your browser&apos;s bookmarks bar. On any page (a protocol, a paper, a
        vendor sheet), click the bookmark: the capture page opens in a new window with the
        page&apos;s title, address, and any text you selected. Check it and press send; nothing
        is saved before that.
      </p>
      <div className="inline">
        <a
          ref={linkRef}
          className="btn-primary"
          draggable="true"
          onClick={(event) => {
            event.preventDefault();
            setHint("Drag it to the bookmarks bar; clicking it here does nothing.");
          }}
        >
          Save to Lab Tracker
        </a>
      </div>
      {hint ? (
        <p className="subtle" role="status">
          {hint}
        </p>
      ) : null}
      <details>
        <summary>Can&apos;t drag it?</summary>
        <p className="subtle">
          Add a bookmark named &ldquo;Save to Lab Tracker&rdquo; and paste this as its address:
        </p>
        <div className="enrollment-url">
          <code className="bookmarklet-source">{source}</code>
          <button type="button" className="btn-secondary" onClick={copySource}>
            Copy bookmarklet
          </button>
        </div>
        {copyStatus ? (
          <span className="subtle" role="status">
            {copyStatus}
          </span>
        ) : null}
      </details>
    </section>
  );
}

export { BookmarkletPanel, captureAppUrl };
