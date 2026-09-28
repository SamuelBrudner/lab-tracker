import * as React from "react";

import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { createMemoryStorage, createUploadQueue } from "../../shared/upload-queue.js";
import { apiResponse, errorResponse, installFetchMock } from "../../test/utils.js";
import { KIOSK_SCAN_HISTORY, KioskCaptureCard, isKioskSearch } from "./KioskCapture.jsx";

const PROJECT = { project_id: "project-1", name: "Rig project" };
const SESSION = {
  project_id: "project-1",
  session_id: "session-1",
  session_type: "operational",
  started_at: "2026-09-28T08:00:00Z",
  status: "active",
};
const OTHER_SESSION = { ...SESSION, session_id: "session-2", started_at: "2026-09-28T09:00:00Z" };

function installNotesRoute({ created, offline = { value: false }, reject = null }) {
  return installFetchMock([
    {
      method: "POST",
      match: "/notes",
      response: (request) => {
        if (offline.value) {
          throw new TypeError("Failed to fetch");
        }
        if (reject && reject.value) {
          return errorResponse("Project is archived.", 409);
        }
        const body = JSON.parse(request.init.body);
        created.push(body);
        return apiResponse({ note_id: `note-${created.length}` }, 201);
      },
    },
  ]);
}

function memoryQueue() {
  return createUploadQueue({
    storage: createMemoryStorage(),
    fetch: (...args) => globalThis.fetch(...args),
  });
}

function renderKiosk(overrides = {}) {
  const props = {
    token: "token-1",
    ownerId: "owner-1",
    authEnabled: true,
    canWrite: true,
    projects: [PROJECT],
    selectedProjectId: PROJECT.project_id,
    onSelectedProjectChange: vi.fn(),
    sessions: [SESSION, OTHER_SESSION],
    navigate: vi.fn(),
    queue: memoryQueue(),
    now: () => Date.parse("2026-09-28T10:15:00Z"),
    ...overrides,
  };
  return { props, ...render(<KioskCaptureCard {...props} />) };
}

function scanInput() {
  return screen.getByLabelText(/scan a barcode/i);
}

function scan(value) {
  const input = scanInput();
  fireEvent.change(input, { target: { value } });
  fireEvent.submit(input.closest("form"));
}

function recentScans() {
  return within(screen.getByRole("list", { name: "Recent scans" })).getAllByRole("listitem");
}

describe("isKioskSearch", () => {
  it("recognizes only kiosk=1", () => {
    expect(isKioskSearch("?kiosk=1&project_id=p")).toBe(true);
    expect(isKioskSearch("?kiosk=0")).toBe(false);
    expect(isKioskSearch("")).toBe(false);
  });
});

describe("KioskCaptureCard", () => {
  it("stages each scanned code as a kiosk note in the launch session and refocuses", async () => {
    window.history.replaceState({}, "", "/app/capture?kiosk=1&project_id=project-1&session_id=session-2");
    const created = [];
    installNotesRoute({ created });
    renderKiosk();

    expect(scanInput()).toHaveFocus();
    expect(screen.getByLabelText("Session")).toHaveValue("session-2");

    scan("  LOT-4471  ");

    await waitFor(() => expect(within(recentScans()[0]).getByText("Saved")).toBeInTheDocument());
    expect(created).toHaveLength(1);
    expect(created[0]).toMatchObject({
      project_id: "project-1",
      raw_content: "Bench scan: LOT-4471",
      targets: [{ entity_id: "session-2", entity_type: "session" }],
      metadata: {
        bench_scan_value: "LOT-4471",
        capture_channel: "kiosk",
        capture_kind: "text",
        capture_source: "mobile_capture",
        captured_at: "2026-09-28T10:15:00.000Z",
      },
    });
    expect(created[0].client_capture_id).toEqual(expect.any(String));
    expect(scanInput()).toHaveValue("");
    expect(scanInput()).toHaveFocus();
  });

  it("ignores empty scans", async () => {
    const created = [];
    installNotesRoute({ created });
    renderKiosk();

    scan("   ");

    expect(screen.getByText("No scans yet.")).toBeInTheDocument();
    expect(created).toEqual([]);
  });

  it("queues scans while offline and shows them synced once the queue drains", async () => {
    const created = [];
    const offline = { value: true };
    installNotesRoute({ created, offline });
    const queue = memoryQueue();
    renderKiosk({ queue });

    scan("TUBE-1");

    await waitFor(() =>
      expect(within(recentScans()[0]).getByText("Queued offline")).toBeInTheDocument()
    );
    expect(await queue.pendingCount()).toBe(1);

    offline.value = false;
    await act(async () => {
      window.dispatchEvent(new Event("online"));
    });

    await waitFor(() => expect(within(recentScans()[0]).getByText("Synced")).toBeInTheDocument());
    expect(created).toHaveLength(1);
    expect(created[0].metadata.bench_scan_value).toBe("TUBE-1");
    expect(await queue.pendingCount()).toBe(0);
  });

  it("marks a scan the server refused as failed and retries it under the same capture id", async () => {
    const created = [];
    const reject = { value: true };
    const fetchMock = installNotesRoute({ created, reject });
    renderKiosk();

    scan("PLATE-9");

    await waitFor(() => expect(within(recentScans()[0]).getByText("Failed")).toBeInTheDocument());
    expect(screen.getByText(/Project is archived/)).toBeInTheDocument();

    reject.value = false;
    fireEvent.click(screen.getByRole("button", { name: "Retry scan PLATE-9" }));

    await waitFor(() => expect(within(recentScans()[0]).getByText("Saved")).toBeInTheDocument());
    const ids = fetchMock.mock.calls.map(([, init]) => JSON.parse(init.body).client_capture_id);
    expect(ids).toHaveLength(2);
    expect(ids[0]).toBe(ids[1]);
  });

  it(`keeps only the last ${KIOSK_SCAN_HISTORY} scans on screen`, async () => {
    const created = [];
    installNotesRoute({ created });
    renderKiosk();

    for (let index = 1; index <= KIOSK_SCAN_HISTORY + 3; index += 1) {
      scan(`CODE-${index}`);
    }

    await waitFor(() => expect(created).toHaveLength(KIOSK_SCAN_HISTORY + 3));
    const items = recentScans();
    expect(items).toHaveLength(KIOSK_SCAN_HISTORY);
    expect(items[0]).toHaveTextContent(`CODE-${KIOSK_SCAN_HISTORY + 3}`);
    expect(screen.queryByText("CODE-1")).not.toBeInTheDocument();
  });

  it("bounds the recorded scan value", async () => {
    const created = [];
    installNotesRoute({ created });
    renderKiosk();

    scan("X".repeat(300));

    await waitFor(() => expect(created).toHaveLength(1));
    expect(created[0].metadata.bench_scan_value).toHaveLength(256);
    expect(created[0].metadata.bench_scan_truncated).toBe(true);
  });

  it("remembers the chosen session for the next kiosk start", async () => {
    installNotesRoute({ created: [] });
    const first = renderKiosk();

    fireEvent.change(screen.getByLabelText("Session"), { target: { value: "session-1" } });
    expect(scanInput()).toHaveFocus();
    first.unmount();

    renderKiosk();
    await waitFor(() => expect(screen.getByLabelText("Session")).toHaveValue("session-1"));
  });

  it("still records scans when this browser refuses local storage", async () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("SecurityError");
    });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("QuotaExceededError");
    });
    const created = [];
    installNotesRoute({ created });
    renderKiosk();

    fireEvent.change(screen.getByLabelText("Session"), { target: { value: "session-1" } });
    scan("VIAL-2");

    await waitFor(() => expect(created).toHaveLength(1));
    expect(created[0].targets).toEqual([{ entity_id: "session-1", entity_type: "session" }]);
  });

  it("exits to the ordinary capture page for the project", () => {
    installNotesRoute({ created: [] });
    const { props } = renderKiosk();

    fireEvent.click(screen.getByRole("button", { name: "Exit kiosk" }));

    expect(props.navigate).toHaveBeenCalledWith("/app/capture?project_id=project-1");
  });

  it("holds scans without write access instead of going deaf", async () => {
    const created = [];
    installNotesRoute({ created });
    renderKiosk({ canWrite: false, accessStatus: "ready" });

    expect(scanInput()).toBeEnabled();
    expect(scanInput()).toHaveFocus();
    expect(screen.getByText(/need write access/)).toBeInTheDocument();

    scan("TUBE-3");

    expect(within(recentScans()[0]).getByText("Waiting for access")).toBeInTheDocument();
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 10));
    });
    expect(created).toEqual([]);
  });

  it("keeps listening while a non-admin's access loads, then sends the held scans", async () => {
    const created = [];
    installNotesRoute({ created });
    const view = renderKiosk({ canWrite: false, accessStatus: "loading" });

    expect(scanInput()).toBeEnabled();
    expect(scanInput()).toHaveFocus();
    expect(screen.getByText(/Checking your access/)).toBeInTheDocument();
    scan("HELD-1");
    scan("HELD-2");
    expect(recentScans()).toHaveLength(2);
    expect(created).toEqual([]);

    view.rerender(<KioskCaptureCard {...view.props} canWrite accessStatus="ready" />);

    await waitFor(() => expect(created).toHaveLength(2));
    expect(created.map((note) => note.metadata.bench_scan_value).sort()).toEqual([
      "HELD-1",
      "HELD-2",
    ]);
    await waitFor(() =>
      expect(recentScans().every((item) => within(item).queryByText("Saved"))).toBe(true)
    );
    expect(scanInput()).toHaveFocus();

    // A token refresh flips access off and on again: nothing is resent.
    view.rerender(<KioskCaptureCard {...view.props} canWrite={false} accessStatus="loading" />);
    view.rerender(<KioskCaptureCard {...view.props} canWrite accessStatus="ready" />);
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 10));
    });
    expect(created).toHaveLength(2);
    expect(scanInput()).toHaveFocus();
  });

  it("takes the focus once a project is chosen", () => {
    installNotesRoute({ created: [] });
    const view = renderKiosk({ selectedProjectId: "" });
    expect(scanInput()).toBeDisabled();

    view.rerender(<KioskCaptureCard {...view.props} selectedProjectId={PROJECT.project_id} />);

    expect(scanInput()).toBeEnabled();
    expect(scanInput()).toHaveFocus();
  });

  it("sends queued scans as soon as the server answers again, without an online event", async () => {
    // A server restart with the network up: no `online` event ever fires.
    const created = [];
    const offline = { value: true };
    installNotesRoute({ created, offline });
    const queue = memoryQueue();
    renderKiosk({ queue });

    scan("DURING-RESTART");
    await waitFor(() =>
      expect(within(recentScans()[0]).getByText("Queued offline")).toBeInTheDocument()
    );

    offline.value = false;
    scan("AFTER-RESTART");

    await waitFor(() => expect(created).toHaveLength(2));
    await waitFor(() =>
      expect(within(recentScans()[1]).getByText("Synced")).toBeInTheDocument()
    );
    expect(within(recentScans()[0]).getByText("Saved")).toBeInTheDocument();
    expect(await queue.pendingCount()).toBe(0);
  });

  it("marks a queued scan failed, not synced, when a drain elsewhere drops it", async () => {
    const created = [];
    const offline = { value: true };
    const reject = { value: false };
    installNotesRoute({ created, offline, reject });
    const queue = memoryQueue();
    renderKiosk({ queue });

    scan("REFUSED-1");
    await waitFor(() =>
      expect(within(recentScans()[0]).getByText("Queued offline")).toBeInTheDocument()
    );

    // The app shell's boot/online retry drains the same queue.
    offline.value = false;
    reject.value = true;
    await act(async () => {
      await queue.drain({ token: "token-1", ownerId: "owner-1", authEnabled: true });
    });

    await waitFor(() =>
      expect(within(recentScans()[0]).getByText("Failed")).toBeInTheDocument()
    );
    expect(within(recentScans()[0]).queryByText("Synced")).toBeNull();
    expect(screen.getByText(/The server refused this scan/)).toBeInTheDocument();
  });
});
