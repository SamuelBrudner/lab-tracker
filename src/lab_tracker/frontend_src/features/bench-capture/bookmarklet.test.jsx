import * as React from "react";

import { act, fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { BookmarkletPanel } from "./BookmarkletPanel.jsx";
import {
  CLIP_TEXT_MAX_CHARS,
  bookmarkletSource,
  clipComposerText,
  clipMetadata,
  readBookmarkletClip,
  sanitizeClipUrl,
} from "./bookmarklet.js";

// Runs the bookmarklet the way a browser would, against a fake visited page.
function runBookmarklet(source, { title, href, selection }) {
  const opened = [];
  const fakeWindow = {
    getSelection: () => ({ toString: () => selection }),
    open: (...args) => {
      opened.push(args);
      return null;
    },
  };
  const script = source.replace(/^javascript:/, "");
  new Function("window", "document", "location", "URLSearchParams", script)(
    fakeWindow,
    { title },
    { href },
    URLSearchParams
  );
  return opened;
}

describe("sanitizeClipUrl", () => {
  it("drops credentials and redacts secret-looking query values", () => {
    expect(
      sanitizeClipUrl("https://user:pw@vendor.example/sheet?id=42&api_key=abc&token=xyz#methods")
    ).toBe("https://vendor.example/sheet?id=42&api_key=REDACTED&token=REDACTED#methods");
  });

  it("drops a fragment that carries key=value pairs", () => {
    expect(sanitizeClipUrl("https://app.example/cb#access_token=abc&state=1")).toBe(
      "https://app.example/cb"
    );
  });

  it("refuses non-web URLs and falls back to the bare page for very long ones", () => {
    expect(sanitizeClipUrl("javascript:alert(1)")).toBe("");
    expect(sanitizeClipUrl("file:///etc/passwd")).toBe("");
    expect(sanitizeClipUrl("not a url")).toBe("");
    expect(sanitizeClipUrl(`https://paper.example/doc?q=${"x".repeat(3000)}`)).toBe(
      "https://paper.example/doc"
    );
  });
});

describe("readBookmarkletClip", () => {
  it("reads a bounded, sanitized clip from the fragment", () => {
    const params = new URLSearchParams({
      "lt-clip": "1",
      text: "y".repeat(CLIP_TEXT_MAX_CHARS + 50),
      title: "Buffer recipe",
      url: "https://me:pw@protocols.example/p/7?session_id=s3cret",
    });
    const clip = readBookmarkletClip(`#${params.toString()}`);

    expect(clip.title).toBe("Buffer recipe");
    expect(clip.url).toBe("https://protocols.example/p/7?session_id=REDACTED");
    expect(clip.text).toHaveLength(CLIP_TEXT_MAX_CHARS);
    expect(clipMetadata(clip)).toEqual({
      share_title: "Buffer recipe",
      share_url: "https://protocols.example/p/7?session_id=REDACTED",
    });
    expect(clipComposerText({ text: "pH 7.4", title: "Buffer", url: "https://a.example/" })).toBe(
      "Buffer\n\nhttps://a.example/\n\n“pH 7.4”"
    );
  });

  it("ignores ordinary anchors and empty clips", () => {
    expect(readBookmarkletClip("#methods")).toBeNull();
    expect(readBookmarkletClip("")).toBeNull();
    expect(readBookmarkletClip("#lt-clip=1")).toBeNull();
  });
});

describe("bookmarkletSource", () => {
  it("opens the capture page in a new window with the page details in the fragment", () => {
    const source = bookmarkletSource("https://lab.example/app/capture");
    expect(source.startsWith("javascript:")).toBe(true);

    const opened = runBookmarklet(source, {
      href: "https://protocols.example/p/7",
      selection: "Incubate 30 min",
      title: "Buffer recipe",
    });

    expect(opened).toHaveLength(1);
    const [url, target, features] = opened[0];
    expect(target).toBe("_blank");
    expect(features).toContain("noopener");
    const parsed = new URL(url);
    expect(`${parsed.origin}${parsed.pathname}`).toBe("https://lab.example/app/capture");
    // Nothing about the visited page goes into the query the server would log.
    expect(parsed.search).toBe("");
    expect(readBookmarkletClip(parsed.hash)).toEqual({
      text: "Incubate 30 min",
      title: "Buffer recipe",
      url: "https://protocols.example/p/7",
    });
  });
});

describe("BookmarkletPanel", () => {
  it("renders a draggable bookmark whose target is the bookmarklet", () => {
    render(<BookmarkletPanel />);

    const link = screen.getByText("Save to Lab Tracker");
    expect(link).toHaveAttribute("draggable", "true");
    const href = link.getAttribute("href");
    expect(href.startsWith("javascript:")).toBe(true);
    expect(href).toContain(JSON.stringify(`${window.location.origin}/app/capture`));
  });

  it("does nothing but explain itself when clicked in place", () => {
    const open = vi.spyOn(window, "open").mockImplementation(() => null);
    render(<BookmarkletPanel />);

    fireEvent.click(screen.getByText("Save to Lab Tracker"));

    expect(open).not.toHaveBeenCalled();
    expect(screen.getByText(/Drag it to the bookmarks bar/)).toBeInTheDocument();
  });

  it("copies the bookmarklet for people who cannot drag", async () => {
    const writeText = vi.fn(async () => undefined);
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText } });
    render(<BookmarkletPanel />);

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Copy bookmarklet" }));
    });

    expect(writeText).toHaveBeenCalledWith(expect.stringMatching(/^javascript:/));
    expect(screen.getByText("Bookmarklet copied.")).toBeInTheDocument();
    delete navigator.clipboard;
  });
});
