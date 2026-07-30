import * as React from "react";

import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { clearAllLocalDrafts, DRAFT_KEY_PREFIX, useLocalDraft } from "./useLocalDraft.js";

const { useState } = React;

function Editor({ draftKey = "field-1", baseline = "saved text", enabled = true }) {
  const [value, setValue] = useState(baseline);
  const { clear, discard, recoveredAt, recoveredValue, restore } = useLocalDraft({
    baseline,
    enabled,
    key: draftKey,
    value,
  });

  return (
    <div>
      <textarea aria-label="Body" value={value} onChange={(e) => setValue(e.target.value)} />
      <span data-testid="recovered">{recoveredValue ?? "none"}</span>
      <span data-testid="recovered-at">{recoveredAt ? "yes" : "no"}</span>
      <button
        type="button"
        onClick={() => {
          const restored = restore();
          if (restored !== null) {
            setValue(restored);
          }
        }}
      >
        Restore
      </button>
      <button type="button" onClick={discard}>
        Discard
      </button>
      <button type="button" onClick={clear}>
        Clear
      </button>
    </div>
  );
}

function storedFor(key) {
  const raw = window.localStorage.getItem(`${DRAFT_KEY_PREFIX}${key}`);
  return raw ? JSON.parse(raw) : null;
}

describe("useLocalDraft", () => {
  beforeEach(() => {
    window.localStorage.clear();
  });

  afterEach(() => {
    vi.restoreAllMocks();
    window.localStorage.clear();
  });

  it("persists text that diverges from what was last saved", async () => {
    render(<Editor />);

    await act(async () => {
      screen.getByLabelText("Body").focus();
    });
    const textarea = screen.getByLabelText("Body");
    fireEvent.change(textarea, { target: { value: "half a paragraph" } });

    expect(storedFor("field-1").value).toBe("half a paragraph");
  });

  it("removes the draft once the text matches what was saved", async () => {
    render(<Editor />);
    const textarea = screen.getByLabelText("Body");

    fireEvent.change(textarea, { target: { value: "diverged" } });
    expect(storedFor("field-1")).not.toBeNull();

    fireEvent.change(textarea, { target: { value: "saved text" } });
    expect(storedFor("field-1")).toBeNull();
  });

  it("offers a draft left behind by an earlier session without applying it", () => {
    window.localStorage.setItem(
      `${DRAFT_KEY_PREFIX}field-1`,
      JSON.stringify({ savedAt: 1737000000000, value: "recovered prose" })
    );

    render(<Editor />);

    expect(screen.getByTestId("recovered")).toHaveTextContent("recovered prose");
    // Crucially not applied on its own — the stored text may be older than what
    // the server now holds.
    expect(screen.getByLabelText("Body")).toHaveValue("saved text");
  });

  it("applies a recovered draft only when the caller restores it", async () => {
    window.localStorage.setItem(
      `${DRAFT_KEY_PREFIX}field-1`,
      JSON.stringify({ savedAt: 1737000000000, value: "recovered prose" })
    );
    render(<Editor />);

    await act(async () => {
      screen.getByRole("button", { name: "Restore" }).click();
    });

    expect(screen.getByLabelText("Body")).toHaveValue("recovered prose");
    expect(screen.getByTestId("recovered-at")).toHaveTextContent("no");
  });

  it("discards a recovered draft from storage on request", async () => {
    window.localStorage.setItem(
      `${DRAFT_KEY_PREFIX}field-1`,
      JSON.stringify({ savedAt: 1737000000000, value: "recovered prose" })
    );
    render(<Editor />);

    await act(async () => {
      screen.getByRole("button", { name: "Discard" }).click();
    });

    expect(storedFor("field-1")).toBeNull();
    expect(screen.getByLabelText("Body")).toHaveValue("saved text");
  });

  it("does not offer a stale draft that matches what was saved", () => {
    window.localStorage.setItem(
      `${DRAFT_KEY_PREFIX}field-1`,
      JSON.stringify({ savedAt: 1737000000000, value: "saved text" })
    );

    render(<Editor />);

    expect(screen.getByTestId("recovered")).toHaveTextContent("none");
  });

  it("ignores a corrupt stored entry", () => {
    window.localStorage.setItem(`${DRAFT_KEY_PREFIX}field-1`, "{not json");

    render(<Editor />);

    expect(screen.getByTestId("recovered")).toHaveTextContent("none");
  });

  it("persists nothing and never throws when storage is unavailable", async () => {
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("QuotaExceededError");
    });
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("SecurityError");
    });

    render(<Editor />);
    const textarea = screen.getByLabelText("Body");
    fireEvent.change(textarea, { target: { value: "typed while storage is blocked" } });

    // The editor still works; only the safety net is missing.
    expect(textarea).toHaveValue("typed while storage is blocked");
    expect(screen.getByTestId("recovered")).toHaveTextContent("none");
  });

  it("stays inert when disabled", async () => {
    render(<Editor enabled={false} />);
    const textarea = screen.getByLabelText("Body");

    fireEvent.change(textarea, { target: { value: "not persisted" } });

    expect(storedFor("field-1")).toBeNull();
  });

  it("clears storage when the caller reports a successful save", async () => {
    render(<Editor />);
    const textarea = screen.getByLabelText("Body");
    fireEvent.change(textarea, { target: { value: "about to be saved" } });
    expect(storedFor("field-1")).not.toBeNull();

    await act(async () => {
      screen.getByRole("button", { name: "Clear" }).click();
    });

    expect(storedFor("field-1")).toBeNull();
  });
});

describe("useLocalDraft survival", () => {
  beforeEach(() => {
    window.localStorage.clear();
  });

  afterEach(() => {
    window.localStorage.clear();
  });

  it("keeps the stored draft on disk while it is only being offered", () => {
    window.localStorage.setItem(
      `${DRAFT_KEY_PREFIX}field-1`,
      JSON.stringify({ savedAt: 1737000000000, value: "recovered prose" })
    );

    const { unmount } = render(<Editor />);
    expect(screen.getByTestId("recovered")).toHaveTextContent("recovered prose");
    // The whole point: a reload before clicking Restore must not lose the text.
    expect(storedFor("field-1").value).toBe("recovered prose");

    unmount();
    render(<Editor />);
    expect(screen.getByTestId("recovered")).toHaveTextContent("recovered prose");
  });

  it("does not copy one scope's text into another scope's key", () => {
    const { rerender } = render(<Editor draftKey="note:project-a" baseline="" />);
    const textarea = screen.getByLabelText("Body");
    fireEvent.change(textarea, { target: { value: "Cells looked odd in well C4" } });
    expect(storedFor("note:project-a").value).toBe("Cells looked odd in well C4");

    // Switching projects re-keys the hook while the stale text is still on
    // screen; it must not be stamped under the new project.
    rerender(<Editor draftKey="note:project-b" baseline="" />);

    expect(storedFor("note:project-b")).toBeNull();
  });

  it("keeps protecting text already typed when a recovered draft is discarded", async () => {
    window.localStorage.setItem(
      `${DRAFT_KEY_PREFIX}field-1`,
      JSON.stringify({ savedAt: 1737000000000, value: "recovered prose" })
    );
    render(<Editor />);
    fireEvent.change(screen.getByLabelText("Body"), { target: { value: "freshly typed" } });

    await act(async () => {
      screen.getByRole("button", { name: "Discard" }).click();
    });

    // The recovered text is gone, but what is on screen is still protected.
    expect(storedFor("field-1").value).toBe("freshly typed");
    expect(screen.getByTestId("recovered-at")).toHaveTextContent("no");
  });
});

describe("clearAllLocalDrafts", () => {
  afterEach(() => {
    window.localStorage.clear();
  });

  it("removes every draft but leaves other keys alone", () => {
    window.localStorage.setItem(`${DRAFT_KEY_PREFIX}a`, JSON.stringify({ savedAt: 1, value: "a" }));
    window.localStorage.setItem(`${DRAFT_KEY_PREFIX}b`, JSON.stringify({ savedAt: 1, value: "b" }));
    window.localStorage.setItem("lab-tracker:last-used-project-id", "project-1");

    clearAllLocalDrafts();

    expect(window.localStorage.getItem(`${DRAFT_KEY_PREFIX}a`)).toBeNull();
    expect(window.localStorage.getItem(`${DRAFT_KEY_PREFIX}b`)).toBeNull();
    expect(window.localStorage.getItem("lab-tracker:last-used-project-id")).toBe("project-1");
  });

  it("does not throw when storage is unavailable", () => {
    vi.spyOn(Storage.prototype, "key").mockImplementation(() => {
      throw new Error("SecurityError");
    });

    expect(() => clearAllLocalDrafts()).not.toThrow();
  });
});
