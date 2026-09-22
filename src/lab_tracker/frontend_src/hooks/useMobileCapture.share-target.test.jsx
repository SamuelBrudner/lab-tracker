import { renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { buildApiPath } from "../shared/api.js";
import { paged } from "../test/fixtures.js";
import { installFetchMock } from "../test/utils.js";

const shareMocks = vi.hoisted(() => ({
  getUploadQueue: vi.fn(),
  migrateIncomingShares: vi.fn(),
}));

vi.mock("../shared/register-sw.js", async (importOriginal) => ({
  ...(await importOriginal()),
  getUploadQueue: shareMocks.getUploadQueue,
}));

vi.mock("../shared/share-target-inbox.js", async (importOriginal) => ({
  ...(await importOriginal()),
  migrateIncomingShares: shareMocks.migrateIncomingShares,
}));

import { useMobileCapture } from "./useMobileCapture.js";

const PROJECT_ID = "project-1";

function installProjectRoutes() {
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

describe("useMobileCapture share-target import", () => {
  let consoleError;

  beforeEach(() => {
    installProjectRoutes();
    consoleError = vi.spyOn(console, "error").mockImplementation(() => {});
  });

  afterEach(() => {
    consoleError.mockRestore();
    shareMocks.getUploadQueue.mockReset();
    shareMocks.migrateIncomingShares.mockReset();
  });

  it("drains imported shares under the current session", async () => {
    const queue = {
      drain: vi.fn(async () => ({ dropped: [], stillQueued: [], uploaded: [{}] })),
    };
    shareMocks.getUploadQueue.mockReturnValue(queue);
    shareMocks.migrateIncomingShares.mockResolvedValue({ migrated: 2 });

    const { props } = renderCaptureHook();

    await waitFor(() =>
      expect(queue.drain).toHaveBeenCalledWith({
        token: "token-1",
        ownerId: "owner-1",
        authEnabled: true,
      })
    );
    expect(props.setFlash).toHaveBeenCalledWith("2 shared captures imported.");
    expect(consoleError).not.toHaveBeenCalled();
  });

  it("logs and flashes a drain failure instead of swallowing it", async () => {
    const failure = new Error("IndexedDB transaction aborted");
    const queue = { drain: vi.fn(async () => Promise.reject(failure)) };
    shareMocks.getUploadQueue.mockReturnValue(queue);
    shareMocks.migrateIncomingShares.mockResolvedValue({ migrated: 1 });

    const { props } = renderCaptureHook();

    await waitFor(() =>
      expect(consoleError).toHaveBeenCalledWith("Shared capture upload failed:", failure)
    );
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

    const { props } = renderCaptureHook();

    await waitFor(() =>
      expect(queue.drain).toHaveBeenCalledWith({
        token: "token-1",
        ownerId: "owner-1",
        authEnabled: true,
      })
    );
    expect(consoleError).toHaveBeenCalledWith("Shared capture import failed:", failure);
    expect(props.setFlash).toHaveBeenLastCalledWith(
      "",
      "Shared captures could not be imported: inbox unreadable. " +
        "Shares not yet imported stay in the share inbox and will be retried."
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
    shareMocks.migrateIncomingShares.mockResolvedValue({ migrated: 1 });

    const { props, unmount } = renderCaptureHook();
    await waitFor(() => expect(rejectDrain).toBeTypeOf("function"));
    unmount();
    const failure = new Error("offline");
    rejectDrain(failure);

    // Still logged (never swallowed), but not flashed into a context that is gone.
    await waitFor(() =>
      expect(consoleError).toHaveBeenCalledWith("Shared capture upload failed:", failure)
    );
    expect(props.setFlash).not.toHaveBeenCalledWith("", expect.stringContaining("offline"));
  });
});
