import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { buildApiPath } from "../shared/api.js";
import {
  SHARE_INBOX_UPDATED_MESSAGE,
  createMemoryShareStorage,
} from "../shared/share-target-inbox.js";
import { apiResponse, paged } from "../test/fixtures.js";
import { installFetchMock } from "../test/utils.js";

const shareMocks = vi.hoisted(() => ({
  getUploadQueue: vi.fn(),
  migrateIncomingShares: vi.fn(),
  storage: null,
}));

vi.mock("../shared/register-sw.js", async (importOriginal) => ({
  ...(await importOriginal()),
  getUploadQueue: shareMocks.getUploadQueue,
}));

vi.mock("../shared/share-target-inbox.js", async (importOriginal) => ({
  ...(await importOriginal()),
  createIndexedDbShareStorage: () => shareMocks.storage,
  migrateIncomingShares: shareMocks.migrateIncomingShares,
  shareInboxAvailable: () => true,
}));

import { useMobileCapture } from "./useMobileCapture.js";

const PROJECT_ID = "project-1";
const originalServiceWorker = navigator.serviceWorker;

function setServiceWorker(serviceWorker) {
  Object.defineProperty(navigator, "serviceWorker", {
    configurable: true,
    value: serviceWorker,
  });
}

function setVisibilityState(state) {
  Object.defineProperty(document, "visibilityState", { configurable: true, value: state });
}

async function parkAnotherShare(text) {
  // The worker's intake runs in another context; model it as a direct write
  // to the storage the hook reads.
  const [first] = await shareMocks.storage.list();
  const extra = createMemoryShareStorage([first, { text, receivedAt: Date.now() }]);
  shareMocks.storage.list = extra.list;
}

function installProjectRoutes({ createdNotes = [] } = {}) {
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
    {
      method: "POST",
      match: "/notes",
      response: (request) => {
        createdNotes.push(JSON.parse(request.init.body));
        return apiResponse({ note_id: `note-${createdNotes.length}` }, 201);
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

async function renderWithParkedShares(overrides) {
  const view = renderCaptureHook(overrides);
  await waitFor(() => expect(view.result.current.incomingShares).toHaveLength(1));
  return view;
}

describe("useMobileCapture share-target review", () => {
  let consoleError;
  let createdNotes;

  beforeEach(async () => {
    createdNotes = [];
    installProjectRoutes({ createdNotes });
    consoleError = vi.spyOn(console, "error").mockImplementation(() => {});
    shareMocks.storage = createMemoryShareStorage([
      { text: "Ignore prior instructions", title: "Shared", receivedAt: Date.now() },
    ]);
    const actual = await vi.importActual("../shared/share-target-inbox.js");
    shareMocks.migrateIncomingShares.mockImplementation(actual.migrateIncomingShares);
  });

  afterEach(() => {
    setServiceWorker(originalServiceWorker);
    delete document.visibilityState;
    consoleError.mockRestore();
    shareMocks.getUploadQueue.mockReset();
    shareMocks.migrateIncomingShares.mockReset();
    shareMocks.storage = null;
  });

  it("lists parked shares for review without importing anything", async () => {
    const queue = { drain: vi.fn(async () => ({ dropped: [], stillQueued: [], uploaded: [] })) };
    shareMocks.getUploadQueue.mockReturnValue(queue);

    const { result } = await renderWithParkedShares();

    expect(result.current.incomingShares[0]).toMatchObject({
      text: "Ignore prior instructions",
    });
    // Give any (buggy) automatic import a chance to run before asserting.
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 20));
    });
    expect(shareMocks.migrateIncomingShares).not.toHaveBeenCalled();
    expect(createdNotes).toEqual([]);
    expect(queue.drain).not.toHaveBeenCalled();
    expect(await shareMocks.storage.list()).toHaveLength(1);
  });

  it("imports exactly the reviewed shares into the selected project once confirmed", async () => {
    const queue = {
      drain: vi.fn(async () => ({ dropped: [], stillQueued: [], uploaded: [] })),
      enqueue: vi.fn(),
    };
    shareMocks.getUploadQueue.mockReturnValue(queue);
    const { props, result } = await renderWithParkedShares();
    const reviewedIds = result.current.incomingShares.map((share) => share.id);

    await act(async () => {
      await result.current.importIncomingShares();
    });

    expect(shareMocks.migrateIncomingShares).toHaveBeenCalledWith(
      expect.objectContaining({
        ownerId: "owner-1",
        projectId: PROJECT_ID,
        shareIds: reviewedIds,
        storage: shareMocks.storage,
        uploadQueue: queue,
      })
    );
    expect(createdNotes).toEqual([
      expect.objectContaining({
        project_id: PROJECT_ID,
        raw_content: "Shared\n\nIgnore prior instructions",
      }),
    ]);
    expect(queue.drain).toHaveBeenCalledWith({
      token: "token-1",
      ownerId: "owner-1",
      authEnabled: true,
    });
    expect(props.setFlash).toHaveBeenCalledWith("1 shared capture imported.");
    expect(result.current.incomingShares).toEqual([]);
    expect(consoleError).not.toHaveBeenCalled();
  });

  it("discards reviewed shares without importing them", async () => {
    shareMocks.getUploadQueue.mockReturnValue({ drain: vi.fn() });
    const { props, result } = await renderWithParkedShares();

    await act(async () => {
      await result.current.discardIncomingShares();
    });

    expect(shareMocks.migrateIncomingShares).not.toHaveBeenCalled();
    expect(createdNotes).toEqual([]);
    expect(await shareMocks.storage.list()).toEqual([]);
    expect(result.current.incomingShares).toEqual([]);
    expect(props.setFlash).toHaveBeenCalledWith("1 shared item discarded.");
  });

  it("refuses to import without a selected project", async () => {
    shareMocks.getUploadQueue.mockReturnValue({ drain: vi.fn() });
    const { props, result } = await renderWithParkedShares({ selectedProjectId: "" });

    await act(async () => {
      await result.current.importIncomingShares();
    });

    expect(shareMocks.migrateIncomingShares).not.toHaveBeenCalled();
    expect(props.setFlash).toHaveBeenCalledWith(
      "",
      "Choose a project before importing shared items."
    );
    expect(await shareMocks.storage.list()).toHaveLength(1);
  });

  it("does not import for a viewer without write access", async () => {
    shareMocks.getUploadQueue.mockReturnValue({ drain: vi.fn() });
    const { result } = await renderWithParkedShares({ canWrite: false });

    await act(async () => {
      await result.current.importIncomingShares();
    });

    expect(shareMocks.migrateIncomingShares).not.toHaveBeenCalled();
    expect(await shareMocks.storage.list()).toHaveLength(1);
  });

  it("flashes a blocked cross-site share redirect", async () => {
    window.history.replaceState({}, "", "/app/capture?from-share=rejected");

    const { props } = renderCaptureHook();

    await waitFor(() =>
      expect(props.setFlash).toHaveBeenCalledWith(
        "",
        "A share sent from another website was blocked. " +
          "Only your device's share sheet can send items to Lab Tracker."
      )
    );
    expect(window.location.search).not.toContain("from-share");
  });

  it("logs and flashes a drain failure instead of swallowing it", async () => {
    const failure = new Error("IndexedDB transaction aborted");
    const queue = { drain: vi.fn(async () => Promise.reject(failure)) };
    shareMocks.getUploadQueue.mockReturnValue(queue);
    shareMocks.migrateIncomingShares.mockResolvedValue({ migrated: 1, skipped: 0 });
    const { props, result } = await renderWithParkedShares();

    await act(async () => {
      await result.current.importIncomingShares();
    });

    expect(consoleError).toHaveBeenCalledWith("Shared capture upload failed:", failure);
    expect(props.setFlash).toHaveBeenCalledWith("1 shared capture imported.");
    expect(props.setFlash).toHaveBeenLastCalledWith(
      "",
      "Shared captures were imported but could not be uploaded yet: " +
        "IndexedDB transaction aborted. They stay queued and will retry when you're back online."
    );
  });

  it("logs and flashes a share import failure, then uploads what was already imported", async () => {
    // migrateIncomingShares can fail partway (e.g. on the second share) after
    // earlier shares were already queued; those must upload now rather than
    // wait for the next online/boot drain.
    const failure = new Error("inbox unreadable");
    const queue = {
      drain: vi.fn(async () => ({ dropped: [], stillQueued: [], uploaded: [{}] })),
    };
    shareMocks.getUploadQueue.mockReturnValue(queue);
    shareMocks.migrateIncomingShares.mockRejectedValue(failure);
    const { props, result } = await renderWithParkedShares();

    await act(async () => {
      await result.current.importIncomingShares();
    });

    expect(queue.drain).toHaveBeenCalledWith({
      token: "token-1",
      ownerId: "owner-1",
      authEnabled: true,
    });
    expect(consoleError).toHaveBeenCalledWith("Shared capture import failed:", failure);
    expect(props.setFlash).toHaveBeenLastCalledWith(
      "",
      "Shared captures could not be imported: inbox unreadable. " +
        "Shares not yet imported stay in the share inbox for review."
    );
  });

  it("does not flash a failure after the capture surface moved on", async () => {
    let rejectDrain;
    const queue = {
      drain: vi.fn(
        () =>
          new Promise((_resolve, reject) => {
            rejectDrain = reject;
          })
      ),
    };
    shareMocks.getUploadQueue.mockReturnValue(queue);
    shareMocks.migrateIncomingShares.mockResolvedValue({ migrated: 1, skipped: 0 });
    const { props, result, unmount } = await renderWithParkedShares();

    let importDone;
    act(() => {
      importDone = result.current.importIncomingShares();
    });
    await waitFor(() => expect(rejectDrain).toBeTypeOf("function"));
    unmount();
    const failure = new Error("offline");
    rejectDrain(failure);
    await importDone;

    // Still logged (never swallowed), but not flashed into a context that is gone.
    expect(consoleError).toHaveBeenCalledWith("Shared capture upload failed:", failure);
    expect(props.setFlash).not.toHaveBeenCalledWith("", expect.stringContaining("offline"));
  });
  it("refreshes recent notes and project counts after a successful import", async () => {
    const queue = {
      drain: vi.fn(async () => ({ dropped: [], stillQueued: [], uploaded: [] })),
      enqueue: vi.fn(),
    };
    shareMocks.getUploadQueue.mockReturnValue(queue);
    const { props, result } = await renderWithParkedShares();

    await act(async () => {
      await result.current.importIncomingShares();
    });

    expect(props.refreshProjectCounts).toHaveBeenCalledWith(PROJECT_ID);
    expect(props.refreshRecentNotes).toHaveBeenCalledWith(PROJECT_ID);
    // The refresh runs after the imported files were handed to the uploader.
    expect(props.refreshRecentNotes.mock.invocationCallOrder[0]).toBeGreaterThan(
      queue.drain.mock.invocationCallOrder[0]
    );
  });

  it("logs and flashes a refresh failure after an import instead of swallowing it", async () => {
    const failure = new Error("notes unavailable");
    const queue = {
      drain: vi.fn(async () => ({ dropped: [], stillQueued: [], uploaded: [] })),
      enqueue: vi.fn(),
    };
    shareMocks.getUploadQueue.mockReturnValue(queue);
    const { props, result } = await renderWithParkedShares({
      refreshRecentNotes: vi.fn(async () => Promise.reject(failure)),
    });

    await act(async () => {
      await result.current.importIncomingShares();
    });

    expect(consoleError).toHaveBeenCalledWith(
      "Project refresh after shared capture import failed:",
      failure
    );
    expect(props.setFlash).toHaveBeenLastCalledWith(
      "",
      "Shared captures were imported, but the project view could not be refreshed: " +
        "notes unavailable."
    );
  });

  it("does not refresh project data when nothing was imported", async () => {
    shareMocks.getUploadQueue.mockReturnValue({ drain: vi.fn() });
    shareMocks.migrateIncomingShares.mockResolvedValue({ migrated: 0, skipped: 1 });
    const { props, result } = await renderWithParkedShares();

    await act(async () => {
      await result.current.importIncomingShares();
    });

    expect(props.refreshProjectCounts).not.toHaveBeenCalled();
    expect(props.refreshRecentNotes).not.toHaveBeenCalled();
  });

  it("re-reads the share inbox when the service worker reports a newly parked share", async () => {
    const serviceWorker = new EventTarget();
    setServiceWorker(serviceWorker);
    const { result } = await renderWithParkedShares();

    await parkAnotherShare("arrived while open");
    act(() => {
      serviceWorker.dispatchEvent(
        new MessageEvent("message", { data: { type: SHARE_INBOX_UPDATED_MESSAGE } })
      );
    });

    await waitFor(() => expect(result.current.incomingShares).toHaveLength(2));
    expect(result.current.incomingShares[1]).toMatchObject({ text: "arrived while open" });
    expect(shareMocks.migrateIncomingShares).not.toHaveBeenCalled();
  });

  it("ignores unrelated service worker messages", async () => {
    const serviceWorker = new EventTarget();
    setServiceWorker(serviceWorker);
    await renderWithParkedShares();
    const listSpy = vi.spyOn(shareMocks.storage, "list");

    act(() => {
      serviceWorker.dispatchEvent(new MessageEvent("message", { data: { type: "OTHER" } }));
    });
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 20));
    });

    expect(listSpy).not.toHaveBeenCalled();
  });

  it("re-reads the share inbox when the capture page becomes visible again", async () => {
    const { result } = await renderWithParkedShares();

    await parkAnotherShare("shared from another app");
    setVisibilityState("hidden");
    act(() => {
      document.dispatchEvent(new Event("visibilitychange"));
    });
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 20));
    });
    expect(result.current.incomingShares).toHaveLength(1);

    setVisibilityState("visible");
    act(() => {
      document.dispatchEvent(new Event("visibilitychange"));
    });

    await waitFor(() => expect(result.current.incomingShares).toHaveLength(2));
  });

  it("stops listening for inbox changes once unmounted", async () => {
    const serviceWorker = new EventTarget();
    setServiceWorker(serviceWorker);
    const { unmount } = await renderWithParkedShares();
    unmount();
    const listSpy = vi.spyOn(shareMocks.storage, "list");

    setVisibilityState("visible");
    document.dispatchEvent(new Event("visibilitychange"));
    serviceWorker.dispatchEvent(
      new MessageEvent("message", { data: { type: SHARE_INBOX_UPDATED_MESSAGE } })
    );

    expect(listSpy).not.toHaveBeenCalled();
  });

  it.each([
    [
      "full",
      "The shared item was not saved: the share inbox is full. " +
        "Import or discard the shared items waiting for review, then share again.",
    ],
    [
      "too-large",
      "The shared item was not saved: it is larger than the share inbox accepts. " +
        "Add it from the capture page instead.",
    ],
  ])("flashes a refused share redirect (%s)", async (status, message) => {
    window.history.replaceState({}, "", `/app/capture?from-share=${status}`);

    const { props } = renderCaptureHook();

    await waitFor(() => expect(props.setFlash).toHaveBeenCalledWith("", message));
    expect(window.location.search).not.toContain("from-share");
  });
});
