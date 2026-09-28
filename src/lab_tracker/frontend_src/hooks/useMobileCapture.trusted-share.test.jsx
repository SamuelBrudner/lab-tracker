import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { buildApiPath } from "../shared/api.js";
import {
  SHARE_INBOX_UPDATED_MESSAGE,
  createMemoryShareStorage,
} from "../shared/share-target-inbox.js";
import {
  SHARE_TRUST_KEY,
  grantShareTrust,
  readShareTrust,
} from "../features/bench-capture/trusted-share.js";
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

function installRoutes({ createdNotes, failNotes = false, gate = null }) {
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
        const response = apiResponse({ note_id: `note-${createdNotes.length}` }, 201);
        return gate ? gate.then(() => response) : response;
      },
    },
  ]);
}

function installServiceWorker() {
  const serviceWorker = new EventTarget();
  Object.defineProperty(navigator, "serviceWorker", { configurable: true, value: serviceWorker });
  return serviceWorker;
}

function parkShareAndNotify(serviceWorker, share) {
  const arrived = createMemoryShareStorage([share]);
  shareMocks.storage.list = arrived.list;
  shareMocks.storage.remove = arrived.remove;
  act(() => {
    serviceWorker.dispatchEvent(
      new MessageEvent("message", { data: { type: SHARE_INBOX_UPDATED_MESSAGE } })
    );
  });
}

// Web Locks as the browser grants them: one holder at a time, in order.
function installWebLocks() {
  let tail = Promise.resolve();
  const request = vi.fn((_name, callback) => {
    const run = tail.then(() => callback());
    tail = run.catch(() => {});
    return run;
  });
  Object.defineProperty(navigator, "locks", { configurable: true, value: { request } });
  return request;
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
    delete navigator.locks;
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

  it("gives one share the same capture id when two open pages import it at once", async () => {
    // Two capture tabs, no Web Locks: both reach the POST before either has
    // removed the share, so only the idempotency key keeps it one capture.
    let release;
    const gate = new Promise((resolve) => {
      release = resolve;
    });
    installRoutes({ createdNotes, gate });
    trustSession();

    renderCaptureHook();
    renderCaptureHook();

    await waitFor(() => expect(createdNotes).toHaveLength(2));
    release();
    const ids = createdNotes.map((body) => body.client_capture_id);
    expect(ids[0]).toEqual(expect.stringMatching(/^share-inbox-/));
    expect(new Set(ids).size).toBe(1);
  });

  it("imports a share once across open pages where Web Locks exist", async () => {
    const request = installWebLocks();
    installRoutes({ createdNotes });
    trustSession();

    renderCaptureHook();
    renderCaptureHook();

    await waitFor(() => expect(request).toHaveBeenCalledTimes(2));
    await settle();
    expect(createdNotes).toHaveLength(1);
    expect(await shareMocks.storage.list()).toEqual([]);
  });

  it("stops at once when another tab stops the window", async () => {
    installRoutes({ createdNotes });
    const serviceWorker = installServiceWorker();
    shareMocks.storage = createMemoryShareStorage([]);
    trustSession();
    const { result } = renderCaptureHook();
    await waitFor(() => expect(result.current.shareTrust).not.toBeNull());

    // The other tab's Stop reaches this one as a storage event.
    localStorage.removeItem(SHARE_TRUST_KEY);
    act(() => {
      window.dispatchEvent(new StorageEvent("storage", { key: SHARE_TRUST_KEY }));
    });
    expect(result.current.shareTrust).toBeNull();

    parkShareAndNotify(serviceWorker, { text: "after stop", receivedAt: T0 });

    await waitFor(() => expect(result.current.incomingShares).toHaveLength(1));
    await settle();
    expect(createdNotes).toEqual([]);
  });

  it("re-checks the stored window before importing, even without an event", async () => {
    installRoutes({ createdNotes });
    const serviceWorker = installServiceWorker();
    shareMocks.storage = createMemoryShareStorage([]);
    trustSession();
    const { result } = renderCaptureHook();
    await waitFor(() => expect(result.current.shareTrust).not.toBeNull());

    // Stopped elsewhere, and this page missed the event.
    localStorage.removeItem(SHARE_TRUST_KEY);
    parkShareAndNotify(serviceWorker, { text: "missed stop", receivedAt: T0 });

    await waitFor(() => expect(result.current.incomingShares).toHaveLength(1));
    await settle();
    expect(createdNotes).toEqual([]);
    expect(result.current.shareTrust).toBeNull();
  });

  it("does not import after the window expires between timer ticks", async () => {
    installRoutes({ createdNotes });
    const serviceWorker = installServiceWorker();
    shareMocks.storage = createMemoryShareStorage([]);
    trustSession({ hours: 1 });
    let clock = T0 + 10 * 60 * 1000;
    const { result } = renderCaptureHook({ now: () => clock });
    await waitFor(() => expect(result.current.shareTrust).not.toBeNull());

    clock = T0 + HOUR + 1000;
    parkShareAndNotify(serviceWorker, { text: "too late", receivedAt: clock });

    await waitFor(() => expect(result.current.incomingShares).toHaveLength(1));
    await settle();
    expect(createdNotes).toEqual([]);
    expect(result.current.shareTrust).toBeNull();
  });

  it("refreshes the window when the page becomes visible again", async () => {
    installRoutes({ createdNotes });
    shareMocks.storage = createMemoryShareStorage([]);
    trustSession({ hours: 1 });
    let clock = T0 + 10 * 60 * 1000;
    const { result } = renderCaptureHook({ now: () => clock });
    await waitFor(() => expect(result.current.shareTrust).not.toBeNull());

    clock = T0 + 2 * HOUR;
    Object.defineProperty(document, "visibilityState", { configurable: true, value: "visible" });
    act(() => {
      document.dispatchEvent(new Event("visibilitychange"));
    });
    delete document.visibilityState;

    await waitFor(() => expect(result.current.shareTrust).toBeNull());
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
