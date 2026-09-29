import * as React from "react";

import { CAPTURE_CHANNEL, withCaptureChannel } from "./bench-helpers.js";

const { useEffect, useRef, useState } = React;

function webNfcSupported() {
  return typeof window !== "undefined" && "NDEFReader" in window;
}

function nfcErrorMessage(error) {
  switch (error?.name) {
    case "NotAllowedError":
      return "NFC permission was refused. Allow NFC for this site in the browser settings and try again.";
    case "NotSupportedError":
      return "This phone cannot write NFC tags right now. Check that NFC is turned on.";
    case "NotReadableError":
    case "NetworkError":
      return "The tag could not be written. Hold it still against the phone and try again.";
    default:
      return `The tag could not be written: ${error?.message || "unknown error"}.`;
  }
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

/**
 * "Write NFC tag": store this session's capture link on an NFC sticker at the
 * rig, so tapping it with a paired phone opens capture with the session
 * preselected (and the captures marked capture_channel=nfc). Uses Web NFC
 * where the browser has it (Chrome on Android); anywhere else the same link
 * is shown with a copy button for any NFC writer app. The tag carries a link,
 * never a credential: the phone still needs its own device grant.
 */
function NfcTagWriter({ captureUrl }) {
  const tagUrl = withCaptureChannel(captureUrl, CAPTURE_CHANNEL.NFC);
  const [status, setStatus] = useState("idle"); // idle | waiting | written | error
  const [message, setMessage] = useState("");
  const [copyStatus, setCopyStatus] = useState("");
  const abortRef = useRef(null);
  const supported = webNfcSupported();

  // Leaving the page must not leave the phone waiting to write a tag.
  useEffect(() => () => abortRef.current?.abort(), []);

  if (!tagUrl) {
    return null;
  }

  async function writeTag() {
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    setStatus("waiting");
    setMessage("");
    try {
      const writer = new window.NDEFReader();
      await writer.write(
        { records: [{ recordType: "url", data: tagUrl }] },
        { signal: controller.signal }
      );
      if (abortRef.current === controller) {
        setStatus("written");
      }
    } catch (error) {
      if (abortRef.current !== controller) {
        return;
      }
      if (error?.name === "AbortError") {
        setStatus("idle");
        setMessage("Tag writing cancelled.");
      } else {
        setStatus("error");
        setMessage(nfcErrorMessage(error));
      }
    } finally {
      if (abortRef.current === controller) {
        abortRef.current = null;
      }
    }
  }

  function cancelWrite() {
    abortRef.current?.abort();
  }

  async function copyLink() {
    setCopyStatus(
      (await writeClipboard(tagUrl)) ? "Link copied." : "Copy failed; select the link instead."
    );
  }

  return (
    <section className="stack nfc-tag-writer" aria-labelledby="nfc-tag-title">
      <h4 id="nfc-tag-title">NFC station tag</h4>
      <p className="subtle">
        Put an NFC sticker at the rig. Tapping it with a paired phone opens capture for this
        session, no scanning or typing.
      </p>
      {supported ? (
        status === "waiting" ? (
          <div className="inline">
            <span role="status">Hold an NFC tag against the back of this phone…</span>
            <button type="button" className="btn-secondary" onClick={cancelWrite}>
              Cancel
            </button>
          </div>
        ) : (
          <div className="inline">
            <button type="button" className="btn-secondary" onClick={writeTag}>
              Write NFC tag
            </button>
          </div>
        )
      ) : (
        <p className="subtle">
          This browser cannot write NFC tags (Web NFC needs Chrome on Android). Copy the link
          below and write it as a URL record with any NFC writer app, for example NFC Tools:
          Write, Add a record, URL, paste the link, Write, then hold the tag to the phone.
        </p>
      )}
      {status === "written" ? (
        <p className="subtle" role="status">
          Tag written. Tapping it opens capture for this session.
        </p>
      ) : null}
      {message ? (
        <p className={status === "error" ? "flash error" : "subtle"} role="status">
          {message}
        </p>
      ) : null}
      <div className="enrollment-url">
        <code>{tagUrl}</code>
        <button type="button" className="btn-secondary" onClick={copyLink}>
          Copy link
        </button>
      </div>
      {copyStatus ? (
        <span className="subtle" role="status">
          {copyStatus}
        </span>
      ) : null}
    </section>
  );
}

export { NfcTagWriter, webNfcSupported };
