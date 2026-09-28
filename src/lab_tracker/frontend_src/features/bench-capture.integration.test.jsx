import * as React from "react";

import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { indexedDB as fakeIndexedDB } from "fake-indexeddb";

import { App } from "../app-shell.jsx";
import { buildApiPath } from "../shared/api.js";
import { TOKEN_STORAGE_KEY } from "../shared/constants.js";
import { resetUploadQueueForTests } from "../shared/register-sw.js";
import { DB_NAME, STORE } from "../shared/share-target-inbox.js";
import { installFetchMock } from "../test/utils.js";
import {
  activeSessionsPath,
  apiResponse,
  captureAnalysesPath,
  captureClaimsPath,
  datasetCountPath,
  datasetListPath,
  note,
  noteCountPath,
  paged,
  project,
  projectGraph,
  projectGraphPath,
  projectsPath,
  questionCountPath,
  questionListPath,
  recentNotesPath,
  session,
} from "../test/fixtures.js";

const PROJECT_ID = "project-1";
const SESSION = session({ primaryQuestionId: null, sessionType: "operational" });

function captureRoutes(createdNotes) {
  return [
    { match: "/auth/me", response: apiResponse({ role: "admin", username: "sam" }) },
    { match: projectsPath, response: apiResponse([project(PROJECT_ID, "Project One")]) },
    { match: questionListPath(PROJECT_ID), response: paged([]) },
    { match: datasetListPath(PROJECT_ID), response: paged([]) },
    { match: noteCountPath(PROJECT_ID), response: paged([], { limit: 1, offset: 0, total: 0 }) },
    { match: questionCountPath(PROJECT_ID), response: paged([], { limit: 1, offset: 0, total: 0 }) },
    { match: datasetCountPath(PROJECT_ID), response: paged([], { limit: 1, offset: 0, total: 0 }) },
    { match: recentNotesPath(PROJECT_ID), response: paged([], { limit: 5, offset: 0, total: 0 }) },
    { match: activeSessionsPath(PROJECT_ID), response: paged([SESSION]) },
    {
      match: buildApiPath("/graph-drafts", { project_id: PROJECT_ID, limit: 10 }),
      response: paged([]),
    },
    { match: buildApiPath("/notes", { project_id: PROJECT_ID, limit: 10 }), response: paged([]) },
    { match: captureAnalysesPath(PROJECT_ID), response: paged([]) },
    { match: captureClaimsPath(PROJECT_ID), response: paged([]) },
    { match: projectGraphPath(PROJECT_ID, "evidence"), response: apiResponse(projectGraph()) },
    {
      match: /^\/projects\/project-1\/(members|access)/,
      response: apiResponse([{ role: "owner", user_id: "user-1" }]),
    },
    {
      method: "POST",
      match: "/notes",
      response: (request) => {
        createdNotes.push(JSON.parse(request.init.body));
        return apiResponse(
          note({ noteId: `note-${createdNotes.length}`, projectId: PROJECT_ID }),
          201
        );
      },
    },
  ];
}

async function parkShare(share) {
  await new Promise((resolve, reject) => {
    const request = fakeIndexedDB.deleteDatabase(DB_NAME);
    request.onsuccess = () => resolve();
    request.onerror = () => reject(request.error);
  });
  const db = await new Promise((resolve, reject) => {
    const request = fakeIndexedDB.open(DB_NAME, 1);
    request.onupgradeneeded = () =>
      request.result.createObjectStore(STORE, { keyPath: "id", autoIncrement: true });
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
  await new Promise((resolve, reject) => {
    const tx = db.transaction(STORE, "readwrite");
    tx.objectStore(STORE).add(share);
    tx.oncomplete = () => resolve();
    tx.onerror = () => reject(tx.error);
  });
  db.close();
}

describe("bench capture in the app", () => {
  it("runs a chrome-free kiosk that stages each scan into the chosen session", async () => {
    localStorage.setItem(TOKEN_STORAGE_KEY, "token-kiosk");
    window.history.replaceState(
      {},
      "",
      `/app/capture?kiosk=1&project_id=${PROJECT_ID}&session_id=session-1`
    );
    const createdNotes = [];
    installFetchMock(captureRoutes(createdNotes));

    const { container } = render(<App />);

    const input = await screen.findByLabelText(/scan a barcode/i);
    await waitFor(() => expect(input).toBeEnabled());
    await waitFor(() => expect(screen.getByLabelText("Session")).toHaveValue("session-1"));
    expect(container.querySelector(".app-shell")).toHaveClass("kiosk-app-shell");
    expect(screen.queryByRole("navigation", { name: "Primary" })).not.toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Dashboard" })).not.toBeInTheDocument();

    fireEvent.change(input, { target: { value: "LOT-77" } });
    fireEvent.submit(input.closest("form"));

    await waitFor(() => expect(createdNotes).toHaveLength(1));
    expect(createdNotes[0]).toMatchObject({
      metadata: { bench_scan_value: "LOT-77", capture_channel: "kiosk" },
      targets: [{ entity_id: "session-1", entity_type: "session" }],
    });

    fireEvent.click(screen.getByRole("button", { name: "Exit kiosk" }));

    expect(await screen.findByRole("navigation", { name: "Primary" })).toBeInTheDocument();
    expect(window.location.search).toBe(`?project_id=${PROJECT_ID}`);
    expect(await screen.findByRole("heading", { name: "Capture" })).toBeInTheDocument();
  });

  it("stamps captures from a page an NFC tag opened", async () => {
    localStorage.setItem(TOKEN_STORAGE_KEY, "token-nfc");
    window.history.replaceState(
      {},
      "",
      `/app/capture?project_id=${PROJECT_ID}&session_id=session-1&capture_channel=nfc`
    );
    const createdNotes = [];
    installFetchMock(captureRoutes(createdNotes));

    render(<App />);

    await waitFor(() =>
      expect(screen.getByLabelText("Session (optional)")).toHaveValue("session-1")
    );
    fireEvent.change(screen.getByLabelText("Message or hint"), {
      target: { value: "Rig 2 back online" },
    });
    const save = screen.getByRole("button", { name: "Save capture" });
    await waitFor(() => expect(save).toBeEnabled());
    fireEvent.click(save);

    await waitFor(() => expect(createdNotes).toHaveLength(1));
    expect(createdNotes[0]).toMatchObject({
      metadata: { capture_channel: "nfc" },
      targets: [{ entity_id: "session-1", entity_type: "session" }],
    });
  });

  it("trusts shares into the selected session for a while, then stops on request", async () => {
    vi.stubGlobal("indexedDB", fakeIndexedDB);
    resetUploadQueueForTests();
    await parkShare({ text: "Western blot notes", receivedAt: Date.now() });
    localStorage.setItem(TOKEN_STORAGE_KEY, "token-trust");
    window.history.replaceState({}, "", `/app/capture?project_id=${PROJECT_ID}&session_id=session-1`);
    const createdNotes = [];
    try {
      installFetchMock(captureRoutes(createdNotes));

      render(<App />);

      const review = await screen.findByRole("region", { name: "Review shared items" });
      const trust = await within(review).findByRole("button", {
        name: /Trust shares into Operational session .* for 2 hours/,
      });
      expect(createdNotes).toEqual([]);

      fireEvent.click(trust);

      await waitFor(() => expect(createdNotes).toHaveLength(1));
      expect(createdNotes[0]).toMatchObject({
        metadata: { capture_channel: "share", capture_source: "share_target" },
        raw_content: "Western blot notes",
        targets: [{ entity_id: "session-1", entity_type: "session" }],
      });
      const banner = await screen.findByText(/go straight into/);
      expect(banner).toHaveTextContent(/without review for (1 h 59 min|2 h) more/);
      await waitFor(() =>
        expect(screen.queryByRole("region", { name: "Review shared items" })).toBeNull()
      );

      fireEvent.click(screen.getByRole("button", { name: "Stop" }));

      await waitFor(() => expect(screen.queryByText(/go straight into/)).toBeNull());
      expect(await screen.findByText(/Trusted shares stopped/)).toBeInTheDocument();
    } finally {
      resetUploadQueueForTests();
      vi.unstubAllGlobals();
    }
  });

  it("offers photo import and a debrief once a session is selected on the capture page", async () => {
    localStorage.setItem(TOKEN_STORAGE_KEY, "token-tools");
    window.history.replaceState({}, "", `/app/capture?project_id=${PROJECT_ID}`);
    installFetchMock(captureRoutes([]));

    render(<App />);

    const sessionSelect = await screen.findByLabelText("Session (optional)");
    await waitFor(() =>
      expect(sessionSelect.querySelector('option[value="session-1"]')).not.toBeNull()
    );
    expect(screen.queryByRole("button", { name: "Import photos" })).toBeNull();

    fireEvent.change(sessionSelect, { target: { value: "session-1" } });

    const tools = await screen.findByRole("region", { name: "Session tools" });
    expect(within(tools).getByRole("button", { name: "Import photos" })).toBeInTheDocument();
    fireEvent.click(within(tools).getByRole("button", { name: "Debrief" }));
    expect(await within(tools).findByText("What would you change next time?")).toBeInTheDocument();
  });
});
