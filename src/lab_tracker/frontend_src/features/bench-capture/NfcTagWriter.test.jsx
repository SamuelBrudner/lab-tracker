import * as React from "react";

import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { NfcTagWriter } from "./NfcTagWriter.jsx";

const CAPTURE_URL = "http://lab.example/app/capture?project_id=project-1&session_id=session-1";
const TAG_URL = `${CAPTURE_URL}&capture_channel=nfc`;

function installNdefReader(write) {
  const writes = [];
  class FakeNdefReader {
    write(message, options) {
      writes.push({ message, options });
      return write(message, options);
    }
  }
  window.NDEFReader = FakeNdefReader;
  return writes;
}

function domError(name, message = name) {
  const error = new Error(message);
  error.name = name;
  return error;
}

afterEach(() => {
  delete window.NDEFReader;
});

describe("NfcTagWriter", () => {
  it("writes the session capture link, marked as an NFC capture, as a URL record", async () => {
    const writes = installNdefReader(async () => undefined);
    render(<NfcTagWriter captureUrl={CAPTURE_URL} />);

    fireEvent.click(screen.getByRole("button", { name: "Write NFC tag" }));

    expect(await screen.findByText(/Tag written/)).toBeInTheDocument();
    expect(writes).toHaveLength(1);
    expect(writes[0].message).toEqual({ records: [{ recordType: "url", data: TAG_URL }] });
    expect(writes[0].options.signal).toBeInstanceOf(AbortSignal);
  });

  it("waits for a tag with a Cancel that aborts the write", async () => {
    const writes = installNdefReader(
      (_message, { signal }) =>
        new Promise((_resolve, reject) => {
          signal.addEventListener("abort", () => reject(domError("AbortError")));
        })
    );
    render(<NfcTagWriter captureUrl={CAPTURE_URL} />);

    fireEvent.click(screen.getByRole("button", { name: "Write NFC tag" }));
    expect(await screen.findByText(/Hold an NFC tag/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));

    expect(await screen.findByText("Tag writing cancelled.")).toBeInTheDocument();
    expect(writes[0].options.signal.aborted).toBe(true);
    expect(screen.getByRole("button", { name: "Write NFC tag" })).toBeInTheDocument();
  });

  it("explains a refused NFC permission", async () => {
    installNdefReader(async () => {
      throw domError("NotAllowedError");
    });
    render(<NfcTagWriter captureUrl={CAPTURE_URL} />);

    fireEvent.click(screen.getByRole("button", { name: "Write NFC tag" }));

    expect(await screen.findByText(/NFC permission was refused/)).toBeInTheDocument();
  });

  it("aborts a pending write when the page goes away", async () => {
    const writes = installNdefReader(() => new Promise(() => {}));
    const { unmount } = render(<NfcTagWriter captureUrl={CAPTURE_URL} />);

    fireEvent.click(screen.getByRole("button", { name: "Write NFC tag" }));
    await screen.findByText(/Hold an NFC tag/);
    unmount();

    expect(writes[0].options.signal.aborted).toBe(true);
  });

  it("offers the link and writer-app instructions without Web NFC", async () => {
    const writeText = vi.fn(async () => undefined);
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText },
    });
    render(<NfcTagWriter captureUrl={CAPTURE_URL} />);

    expect(screen.queryByRole("button", { name: "Write NFC tag" })).not.toBeInTheDocument();
    expect(screen.getByText(/any NFC writer app/)).toBeInTheDocument();
    expect(screen.getByText(TAG_URL)).toBeInTheDocument();

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Copy link" }));
    });

    expect(writeText).toHaveBeenCalledWith(TAG_URL);
    await waitFor(() => expect(screen.getByText("Link copied.")).toBeInTheDocument());
    delete navigator.clipboard;
  });

  it("says so when the clipboard is unavailable", async () => {
    render(<NfcTagWriter captureUrl={CAPTURE_URL} />);

    fireEvent.click(screen.getByRole("button", { name: "Copy link" }));

    expect(await screen.findByText(/Copy failed/)).toBeInTheDocument();
  });
});
