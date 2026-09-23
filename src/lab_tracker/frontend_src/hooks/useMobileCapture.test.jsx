import { act, renderHook, waitFor } from "@testing-library/react";

import { buildApiPath } from "../shared/api.js";
import { apiResponse, note, paged } from "../test/fixtures.js";
import { errorResponse, installFetchMock } from "../test/utils.js";

import { useMobileCapture } from "./useMobileCapture.js";

const PROJECT_ID = "project-1";

function deferred() {
  let resolve;
  const promise = new Promise((innerResolve) => {
    resolve = innerResolve;
  });
  return { promise, resolve };
}

function installCaptureRoutes(createNote) {
  return installFetchMock([
    {
      match: buildApiPath("/graph-drafts", { project_id: PROJECT_ID, limit: 10 }),
      response: paged([]),
    },
    {
      match: buildApiPath("/notes", { project_id: PROJECT_ID, limit: 10 }),
      response: paged([]),
    },
    {
      match: buildApiPath("/analyses", { project_id: PROJECT_ID, limit: 50 }),
      response: paged([]),
    },
    {
      match: buildApiPath("/claims", { project_id: PROJECT_ID, limit: 50 }),
      response: paged([]),
    },
    { match: "/notes", method: "POST", response: createNote },
  ]);
}

function renderCaptureHook() {
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
  };
  return { props, ...renderHook(() => useMobileCapture(props)) };
}

describe("useMobileCapture", () => {
  it("ignores a second upload while the first is still in flight", async () => {
    const pendingCreate = deferred();
    const fetchMock = installCaptureRoutes(() => pendingCreate.promise);
    const { props, result } = renderCaptureHook();

    act(() => {
      result.current.handleComposerTextChange({ target: { value: "Fly 12 climbed" } });
    });
    expect(result.current.uploading).toBe(false);

    // Two taps delivered before React re-renders: both calls see the same
    // render closure, so only a synchronous guard can stop the second one.
    let first;
    let second;
    act(() => {
      first = result.current.uploadCapture();
      second = result.current.uploadCapture();
    });
    expect(result.current.uploading).toBe(true);

    await act(async () => {
      pendingCreate.resolve(apiResponse(note({ noteId: "note-1" }), 201));
      await Promise.all([first, second]);
    });

    const creates = fetchMock.mock.calls.filter(
      ([url, init]) => url === "/notes" && init?.method === "POST"
    );
    expect(creates).toHaveLength(1);
    expect(props.setFlash).toHaveBeenCalledWith("Capture saved for review.");
    await waitFor(() => expect(result.current.uploading).toBe(false));
  });

  it("releases the in-flight guard after a failed upload so the user can retry", async () => {
    let attempts = 0;
    installCaptureRoutes(() => {
      attempts += 1;
      if (attempts === 1) {
        return errorResponse("Server hiccup.", 500);
      }
      return apiResponse(note({ noteId: "note-2" }), 201);
    });
    const { props, result } = renderCaptureHook();

    act(() => {
      result.current.handleComposerTextChange({ target: { value: "Retry me" } });
    });
    await act(async () => {
      await result.current.uploadCapture();
    });
    expect(result.current.uploading).toBe(false);

    await act(async () => {
      await result.current.uploadCapture();
    });

    expect(attempts).toBe(2);
    expect(props.setFlash).toHaveBeenLastCalledWith("Capture saved for review.");
  });

  it("is not ready to save again once a capture has been saved", async () => {
    const fetchMock = installCaptureRoutes(() => apiResponse(note({ noteId: "note-3" }), 201));
    const { props, result } = renderCaptureHook();

    act(() => {
      result.current.handleComposerTextChange({ target: { value: "Saved once" } });
    });
    await act(async () => {
      await result.current.uploadCapture();
    });
    expect(props.setFlash).toHaveBeenLastCalledWith("Capture saved for review.");
    expect(result.current.composerTextValue()).toBe("");
    expect(result.current.readyToCapture()).toBe(false);

    await act(async () => {
      await result.current.uploadCapture();
    });

    expect(props.setFlash).toHaveBeenLastCalledWith(
      "",
      "Choose the required capture input before upload."
    );
    const creates = fetchMock.mock.calls.filter(
      ([url, init]) => url === "/notes" && init?.method === "POST"
    );
    expect(creates).toHaveLength(1);
  });
});
