import { act, renderHook, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { buildApiPath } from "../shared/api.js";
import { apiResponse, note, paged } from "../test/fixtures.js";
import { installFetchMock } from "../test/utils.js";

import { useMobileCapture } from "./useMobileCapture.js";

const PROJECT_ID = "project-1";

function installCaptureRoutes(created) {
  return installFetchMock([
    {
      match: buildApiPath("/graph-drafts", { project_id: PROJECT_ID, limit: 10 }),
      response: paged([]),
    },
    { match: buildApiPath("/notes", { project_id: PROJECT_ID, limit: 10 }), response: paged([]) },
    {
      match: buildApiPath("/analyses", { project_id: PROJECT_ID, limit: 50 }),
      response: paged([]),
    },
    { match: buildApiPath("/claims", { project_id: PROJECT_ID, limit: 50 }), response: paged([]) },
    {
      match: "/notes",
      method: "POST",
      response: (request) => {
        created.push(JSON.parse(request.init.body));
        return apiResponse(note({ noteId: `note-${created.length}` }), 201);
      },
    },
  ]);
}

function renderCaptureHook(overrides = {}) {
  const props = {
    token: "token-1",
    ownerId: "owner-1",
    canWrite: true,
    selectedProjectId: PROJECT_ID,
    questions: [],
    navigate: vi.fn(),
    setBusy: vi.fn(),
    setFlash: vi.fn(),
    refreshProjectCounts: vi.fn(async () => undefined),
    refreshRecentNotes: vi.fn(async () => undefined),
    ...overrides,
  };
  return { props, ...renderHook(() => useMobileCapture(props)) };
}

describe("useMobileCapture bench capture channels", () => {
  it("marks captures from a page opened by an NFC tag", async () => {
    const created = [];
    installCaptureRoutes(created);
    const { result } = renderCaptureHook({ launchCaptureChannel: "nfc" });

    act(() => {
      result.current.handleComposerTextChange({ target: { value: "Rig 2 warm" } });
    });
    await act(async () => {
      await result.current.uploadCapture();
    });

    expect(created[0].metadata.capture_channel).toBe("nfc");
  });

  it("prefills from a bookmarklet clip, saves it only on send, as a pointer", async () => {
    const params = new URLSearchParams({
      "lt-clip": "1",
      text: "Incubate 30 min at 37 C",
      title: "Buffer recipe",
      url: "https://user:pw@protocols.example/p/7?token=abc",
    });
    window.history.replaceState({}, "", `/app/capture#${params.toString()}`);
    const created = [];
    const fetchMock = installCaptureRoutes(created);

    const { props, result } = renderCaptureHook();

    await waitFor(() => expect(result.current.clip).not.toBeNull());
    expect(result.current.composerTextValue()).toBe(
      "Buffer recipe\n\nhttps://protocols.example/p/7?token=REDACTED\n\n“Incubate 30 min at 37 C”"
    );
    // The page details leave the address bar, and nothing was sent yet.
    expect(window.location.hash).toBe("");
    expect(
      fetchMock.mock.calls.filter(([, init]) => init?.method === "POST")
    ).toHaveLength(0);

    await act(async () => {
      await result.current.uploadCapture();
    });

    expect(created).toHaveLength(1);
    expect(created[0].metadata).toMatchObject({
      capture_channel: "bookmarklet",
      capture_kind: "text",
      share_title: "Buffer recipe",
      share_url: "https://protocols.example/p/7?token=REDACTED",
    });
    expect(props.setFlash).toHaveBeenCalledWith("Capture saved for review.");
    expect(result.current.clip).toBeNull();
  });

  it("ignores an ordinary fragment", async () => {
    window.history.replaceState({}, "", "/app/capture#top");
    installCaptureRoutes([]);

    const { result } = renderCaptureHook();

    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 10));
    });
    expect(result.current.clip).toBeNull();
    expect(result.current.composerTextValue()).toBe("");
    expect(window.location.hash).toBe("#top");
  });
});
