import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { buildApiPath } from "../shared/api.js";
import {
  SHARE_INBOX_UPDATED_MESSAGE,
  createMemoryShareStorage,
} from "../shared/share-target-inbox.js";
import { grantShareTrust, readShareTrust } from "../features/bench-capture/trusted-share.js";
import { apiResponse, paged } from "../test/fixtures.js";
import { errorResponse, installFetchMock } from "../test/utils.js";

const shareMocks = vi.hoisted(() => ({
  getUploadQueue: vi.fn(),
  storage: null,
}));

vi.mock("../shared/register-sw.js", async (importOriginal) => ({
  ...(await importOriginal()),
  getUploadQueue: shareMocks.getUploadQueue,
}));

vi.mock("../shared/share-target-inbox.js", async (importOriginal) => ({
  ...(await importOriginal()),
  createIndexedDbShareStorage: () => shareMocks.storage,
  shareInboxAvailable: () => true,
}));

import { useMobileCapture } from "./useMobileCapture.js";

const PROJECT_ID = "project-1";
const HOUR = 60 * 60 * 1000;
const T0 = Date.parse("2026-09-28T10:00:00Z");
const SESSION = {
  project_id: PROJECT_ID,
  session_id: "session-1",
  session_type: "operational",
  started_at: "2026-09-28T08:00:00Z",
  status: "active",
};
const SESSIONS = [SESSION];
const originalServiceWorker = navigator.serviceWorker;

function installRoutes({ createdNotes, failNotes = false }) {
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
      method: "POST",
      match: "/notes",
      response: (request) => {
        if (failNotes) {
          return errorResponse("Server is busy.", 500);
        }
        createdNotes.push(JSON.parse(request.init.body));
        return apiResponse({ note_id: `note-${createdNotes.length}` }, 201);
      },
    },
  ]);
}

function trustSession({ projectId = PROJECT_ID, hours = 2, now = T0 } = {}) {
  return grantShareTrust({
    hours,
    now,
    ownerId: "owner-1",
    projectId,
    sessionId: SESSION.session_id,
    sessionLabel: "Rig 2 session",
  });
}

function renderCaptureHook(overrides = {}) {
  const props = {
    token: "token-1",
    ownerId: "owner-1",
    canWrite: true,
    selectedProjectId: PROJECT_ID,
    questions: [],
    sessions: SESSIONS,
    navigate: vi.fn(),
    setBusy: vi.fn(),
    setFlash: vi.fn(),
    refreshProjectCounts: vi.fn(async () => undefined),
    refreshRecentNotes: vi.fn(async () => undefined),
    now: () => T0 + 10 * 60 * 1000,
    ...overrides,
  };
  return { props, ...renderHook(() => useMobileCapture(props)) };
}

async function settle() {
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 20));
  });
}

describe("useMobileCapture trusted share window", () => {
  let createdNotes;
  let queue;
  let consoleError;

  beforeEach(() => {
    createdNotes = [];
    queue = {
      drain: vi.fn(async () => ({ dropped: [], stillQueued: [], uploaded: [] })),
      enqueue: vi.fn(async () => 1),
    };
    shareMocks.getUploadQueue.mockReturnValue(queue);
    shareMocks.storage = createMemoryShareStorage([
      { text: "Gel image notes", title: "Shared", receivedAt: T0 },
    ]);
    consoleError = vi.spyOn(console, "error").mockImplementation(() => {});
  });

  afterEach(() => {
    Object.defineProperty(navigator, "serviceWorker", {
      configurable: true,
      value: originalServiceWorker,
    });
    consoleError.mockRestore();
    shareMocks.getUploadQueue.mockReset();
    shareMocks.storage = null;
  });

  it("imports shares straight into the trusted session with capture_channel=share", async () => {
    installRoutes({ createdNotes });
    trustSession();

    const { props, result } = renderCaptureHook();

    await waitFor(() => expect(createdNotes).toHaveLength(1));
    expect(createdNotes[0]).toMatchObject({
      project_id: PROJECT_ID,
      raw_content: "Shared\n\nGel image notes",
      targets: [{ entity_id: "session-1", entity_type: "session" }],
      metadata: { capture_channel: "share", capture_source: "share_target" },
    });
    await waitFor(() => expect(result.current.incomingShares).toEqual([]));
    expect(props.setFlash).toHaveBeenCalledWith("1 shared capture saved into Rig 2 session.");
    expect(result.current.shareTrust).toMatchObject({ sessionId: "session-1" });
    expect(await shareMocks.storage.list()).toEqual([]);
  });

  it("also imports file shares with the session target through the offline queue", async () => {
    installRoutes({ createdNotes });
    shareMocks.storage = createMemoryShareStorage([
      {
        contentType: "image/jpeg",
        file: new File(["jpeg"], "gel.jpg", { type: "image/jpeg" }),
        filename: "gel.jpg",
        receivedAt: T0,
      },
    ]);
    trustSession();

    renderCaptureHook();

    await waitFor(() => expect(queue.enqueue).toHaveBeenCalledTimes(1));
    const job = queue.enqueue.mock.calls[0][0];
    expect(JSON.parse(job.fields.targets)).toEqual([
      { entity_id: "session-1", entity_type: "session" },
    ]);
    expect(JSON.parse(job.fields.metadata)).toMatchObject({ capture_channel: "share" });
    await waitFor(() => expect(queue.drain).toHaveBeenCalled());
  });

  it("imports a share that arrives while the page is open", async () => {
    installRoutes({ createdNotes });
    const serviceWorker = new EventTarget();
    Object.defineProperty(navigator, "serviceWorker", { configurable: true, value: serviceWorker });
    shareMocks.storage = createMemoryShareStorage([]);
    trustSession();
    const { result } = renderCaptureHook();
    await settle();
    expect(createdNotes).toEqual([]);

    const arrived = createMemoryShareStorage([{ text: "second gel", receivedAt: T0 }]);
    shareMocks.storage.list = arrived.list;
    shareMocks.storage.remove = arrived.remove;
    act(() => {
      serviceWorker.dispatchEvent(
        new MessageEvent("message", { data: { type: SHARE_INBOX_UPDATED_MESSAGE } })
      );
    });

    await waitFor(() => expect(createdNotes).toHaveLength(1));
    expect(createdNotes[0].raw_content).toBe("second gel");
    await waitFor(() => expect(result.current.incomingShares).toEqual([]));
  });

  it("never applies across projects", async () => {
    installRoutes({ createdNotes });
    trustSession({ projectId: "project-2" });

    const { result } = renderCaptureHook();

    await waitFor(() => expect(result.current.incomingShares).toHaveLength(1));
    await settle();
    expect(createdNotes).toEqual([]);
    expect(result.current.shareTrust).toBeNull();
  });

  it("stops at expiry and leaves later shares for review", async () => {
    installRoutes({ createdNotes });
    trustSession({ hours: 1 });

    const { result } = renderCaptureHook({ now: () => T0 + HOUR + 1 });

    await waitFor(() => expect(result.current.incomingShares).toHaveLength(1));
    await settle();
    expect(createdNotes).toEqual([]);
    expect(result.current.shareTrust).toBeNull();
  });

  it("does not apply once the trusted session is no longer active", async () => {
    installRoutes({ createdNotes });
    trustSession();

    const { result } = renderCaptureHook({ sessions: [] });

    await waitFor(() => expect(result.current.incomingShares).toHaveLength(1));
    await settle();
    expect(createdNotes).toEqual([]);
    expect(result.current.shareTrust).toBeNull();
  });

  it("does not apply for a viewer without write access", async () => {
    installRoutes({ createdNotes });
    trustSession();

    const { result } = renderCaptureHook({ canWrite: false });

    await waitFor(() => expect(result.current.incomingShares).toHaveLength(1));
    await settle();
    expect(createdNotes).toEqual([]);
  });

  it("is opened from the review for the selected session and imports what is listed", async () => {
    installRoutes({ createdNotes });
    const { props, result } = renderCaptureHook();
    await waitFor(() => expect(result.current.incomingShares).toHaveLength(1));
    act(() => {
      result.current.setSessionId("session-1");
    });

    act(() => {
      result.current.trustShares(2);
    });

    await waitFor(() => expect(createdNotes).toHaveLength(1));
    expect(createdNotes[0].targets).toEqual([{ entity_id: "session-1", entity_type: "session" }]);
    expect(
      readShareTrust({ ownerId: "owner-1", projectId: PROJECT_ID, now: T0 + 11 * 60 * 1000 })
    ).toMatchObject({ sessionId: "session-1" });
    expect(props.setFlash).toHaveBeenCalledWith(
      expect.stringMatching(/^Shares go straight into Operational session started .* for the next 2 h\.$/)
    );
  });

  it("stops on request, after which shares wait for review again", async () => {
    installRoutes({ createdNotes });
    shareMocks.storage = createMemoryShareStorage([]);
    trustSession();
    const { result } = renderCaptureHook();
    await waitFor(() => expect(result.current.shareTrust).not.toBeNull());

    act(() => {
      result.current.stopShareTrust();
    });

    expect(result.current.shareTrust).toBeNull();
    expect(readShareTrust({ ownerId: "owner-1", projectId: PROJECT_ID, now: T0 })).toBeNull();
  });

  it("leaves a share whose automatic import failed for review instead of retrying in a loop", async () => {
    const fetchMock = installRoutes({ createdNotes, failNotes: true });
    trustSession();

    const { props, result } = renderCaptureHook();

    await waitFor(() =>
      expect(props.setFlash).toHaveBeenCalledWith(
        "",
        expect.stringContaining("Shared captures could not be imported")
      )
    );
    await settle();
    const posts = fetchMock.mock.calls.filter(([, init]) => init?.method === "POST");
    expect(posts).toHaveLength(1);
    expect(result.current.incomingShares).toHaveLength(1);
  });

  it("says so, and trusts nothing, when this browser will not store the window", async () => {
    installRoutes({ createdNotes });
    const { props, result } = renderCaptureHook();
    await waitFor(() => expect(result.current.incomingShares).toHaveLength(1));
    act(() => {
      result.current.setSessionId("session-1");
    });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("QuotaExceededError");
    });

    act(() => {
      result.current.trustShares(1);
    });
    await settle();

    expect(props.setFlash).toHaveBeenCalledWith(
      "",
      expect.stringContaining("would not keep a trusted share window")
    );
    expect(createdNotes).toEqual([]);
    expect(result.current.shareTrust).toBeNull();
  });
});
