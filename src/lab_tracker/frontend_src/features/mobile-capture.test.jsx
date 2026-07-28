import * as React from "react";

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { vi } from "vitest";

import { MobileCaptureCard } from "./mobile-capture.jsx";
import { buildApiPath } from "../shared/api.js";
import { apiResponse, errorResponse, installFetchMock } from "../test/utils.js";

// jsdom has no IndexedDB, so the real getUploadQueue returns null and the
// share migration effect no-ops. These mocks keep that default (queue null)
// while letting the check-in inheritance test inject a fake queue and observe
// which project the migration ran against.
const shareMigration = vi.hoisted(() => ({
  migrate: vi.fn(async () => ({ migrated: 0, skipped: 0 })),
  queue: { current: null },
}));

vi.mock("../shared/register-sw.js", async (importOriginal) => {
  const actual = await importOriginal();
  return {
    ...actual,
    getUploadQueue: () => shareMigration.queue.current,
  };
});

vi.mock("../shared/share-target-inbox.js", async (importOriginal) => {
  const actual = await importOriginal();
  return {
    ...actual,
    migrateIncomingShares: (options) => shareMigration.migrate(options),
  };
});

const UNBOUND_TAG_MESSAGE = "This tag isn't bound yet — capture works as usual.";
const BENCH_CHECKIN_KEY = "lab-tracker:bench-checkin";
const HOUR_MS = 3600000;
const VOICE_READY_STATUS = "Tap to record — capture saves automatically.";

beforeEach(() => {
  localStorage.clear();
  sessionStorage.clear();
  window.history.replaceState({}, "", "/app/capture");
  shareMigration.queue.current = null;
  shareMigration.migrate.mockClear();
});

function project(projectId, name) {
  return { name, project_id: projectId };
}

function session({ projectId = "project-1", sessionId = "session-1" } = {}) {
  return {
    link_code: "ABC123",
    project_id: projectId,
    session_id: sessionId,
    session_type: "scientific",
    started_at: "2026-04-20T03:00:00Z",
    status: "active",
  };
}

function pendingCaptureRoutes(projectId) {
  return [
    {
      match: buildApiPath("/graph-drafts", { project_id: projectId, limit: 10 }),
      response: apiResponse([]),
    },
    {
      match: buildApiPath("/notes", { project_id: projectId, limit: 10 }),
      response: apiResponse([]),
    },
    {
      match: buildApiPath("/analyses", { project_id: projectId, limit: 50 }),
      response: apiResponse([]),
    },
    {
      match: buildApiPath("/claims", { project_id: projectId, limit: 50 }),
      response: apiResponse([]),
    },
  ];
}

function CaptureHarness({
  canWrite = true,
  initialProjectId = "",
  projects = [],
  sessions = [],
  setFlash = () => {},
  token = "token-1",
}) {
  const [selectedProjectId, setSelectedProjectId] = React.useState(initialProjectId);
  return (
    <MobileCaptureCard
      token={token}
      canWrite={canWrite}
      projects={projects}
      selectedProjectId={selectedProjectId}
      onSelectedProjectChange={setSelectedProjectId}
      questions={[]}
      datasets={[]}
      sessions={sessions}
      navigate={() => {}}
      setBusy={() => {}}
      setFlash={setFlash}
      refreshProjectCounts={() => Promise.resolve()}
      refreshRecentNotes={() => Promise.resolve()}
    />
  );
}

function requestedUrls(fetchMock) {
  return fetchMock.mock.calls.map(([input]) => (typeof input === "string" ? input : input.url));
}

function postNoteCalls(fetchMock) {
  return fetchMock.mock.calls.filter(
    ([input, init]) =>
      (typeof input === "string" ? input : input.url) === "/notes" &&
      String(init?.method || "GET").toUpperCase() === "POST"
  );
}

async function settle(ms = 25) {
  await new Promise((resolve) => setTimeout(resolve, ms));
}

// A promise the test resolves by hand, so async effects have a real
// in-flight window instead of resolving before the next assertion.
function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, reject, resolve };
}

function tagBinding(overrides = {}) {
  return {
    hint: null,
    label: "Rig 2",
    log_breadcrumb: false,
    mode: null,
    project_id: "project-2",
    session_id: null,
    slug: "rig-2",
    ...overrides,
  };
}

// Renders the card directly (no harness state) so tests can spy on
// onSelectedProjectChange and control selectedProjectId exactly.
function renderRawCard({
  canWrite = true,
  onSelectedProjectChange = () => {},
  projects = [],
  selectedProjectId = "",
  sessions = [],
  setFlash = () => {},
  token = "token-1",
} = {}) {
  return render(
    <MobileCaptureCard
      token={token}
      canWrite={canWrite}
      projects={projects}
      selectedProjectId={selectedProjectId}
      onSelectedProjectChange={onSelectedProjectChange}
      questions={[]}
      datasets={[]}
      sessions={sessions}
      navigate={() => {}}
      setBusy={() => {}}
      setFlash={setFlash}
      refreshProjectCounts={() => Promise.resolve()}
      refreshRecentNotes={() => Promise.resolve()}
    />
  );
}

describe("MobileCaptureCard capture URL params", () => {
  it("keeps a param-less mount byte-identical to today", async () => {
    let noteBody = null;
    const fetchMock = installFetchMock([
      ...pendingCaptureRoutes("project-1"),
      {
        match: "/notes",
        method: "POST",
        response: (request) => {
          noteBody = JSON.parse(request.init.body);
          return apiResponse({ note_id: "note-99" }, 201);
        },
      },
    ]);
    const setFlash = vi.fn();

    render(
      <CaptureHarness
        initialProjectId="project-1"
        projects={[project("project-1", "Project One")]}
        setFlash={setFlash}
      />
    );

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(4));
    expect(requestedUrls(fetchMock).sort()).toEqual(
      [
        buildApiPath("/graph-drafts", { project_id: "project-1", limit: 10 }),
        buildApiPath("/notes", { project_id: "project-1", limit: 10 }),
        buildApiPath("/analyses", { project_id: "project-1", limit: 50 }),
        buildApiPath("/claims", { project_id: "project-1", limit: 50 }),
      ].sort()
    );
    expect(screen.getByLabelText("Short hint (optional)")).toHaveValue("");

    fireEvent.change(screen.getByLabelText("Message or hint"), {
      target: { value: "Plain note" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save capture" }));

    await waitFor(() => expect(setFlash).toHaveBeenCalledWith("Capture saved for review."));
    expect(noteBody.metadata).toEqual({
      capture_kind: "text",
      capture_mode: "text",
      capture_review_status: "pending_review",
      capture_source: "mobile_capture",
    });
  });

  it("prefills project and hint from URL params", async () => {
    let noteBody = null;
    installFetchMock([
      ...pendingCaptureRoutes("project-1"),
      ...pendingCaptureRoutes("project-2"),
      {
        match: "/notes",
        method: "POST",
        response: (request) => {
          noteBody = JSON.parse(request.init.body);
          return apiResponse({ note_id: "note-99" }, 201);
        },
      },
    ]);
    window.history.replaceState({}, "", "/app/capture?project=project-2&hint=Rig%202");

    render(
      <CaptureHarness
        initialProjectId="project-1"
        projects={[project("project-1", "Project One"), project("project-2", "Project Two")]}
      />
    );

    await waitFor(() => expect(screen.getByLabelText("Project")).toHaveValue("project-2"));
    expect(screen.getByLabelText("Short hint (optional)")).toHaveValue("Rig 2");

    fireEvent.change(screen.getByLabelText("Message or hint"), {
      target: { value: "Fly loaded" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save capture" }));

    await waitFor(() => expect(noteBody).not.toBeNull());
    expect(noteBody.project_id).toBe("project-2");
    expect(noteBody.metadata).toEqual({
      capture_entry: "url_params",
      capture_hint: "Rig 2",
      capture_kind: "text",
      capture_mode: "text",
      capture_review_status: "pending_review",
      capture_source: "mobile_capture",
    });
  });

  it("resolves session-link, pre-attaches the session, and lets its project win", async () => {
    installFetchMock([
      ...pendingCaptureRoutes("project-1"),
      ...pendingCaptureRoutes("project-2"),
      {
        match: "/sessions/by-link/ABC123",
        response: apiResponse(session({ projectId: "project-2", sessionId: "session-9" })),
      },
    ]);
    window.history.replaceState({}, "", "/app/capture?session-link=ABC123");

    render(
      <CaptureHarness
        initialProjectId="project-1"
        projects={[project("project-1", "Project One"), project("project-2", "Project Two")]}
        sessions={[session({ projectId: "project-2", sessionId: "session-9" })]}
      />
    );

    await waitFor(() => expect(screen.getByLabelText("Project")).toHaveValue("project-2"));
    await waitFor(() =>
      expect(screen.getByLabelText("Session (optional)")).toHaveValue("session-9")
    );
  });

  it("applies a resolved tag binding to project, session, hint, and metadata", async () => {
    let noteBody = null;
    installFetchMock([
      ...pendingCaptureRoutes("project-1"),
      ...pendingCaptureRoutes("project-2"),
      {
        match: "/tags/rig-2",
        response: apiResponse({
          hint: "Bench 4",
          label: "Rig 2",
          log_breadcrumb: false,
          mode: null,
          project_id: "project-2",
          session_id: "session-9",
          slug: "rig-2",
        }),
      },
      {
        match: "/notes",
        method: "POST",
        response: (request) => {
          noteBody = JSON.parse(request.init.body);
          return apiResponse({ note_id: "note-99" }, 201);
        },
      },
    ]);
    window.history.replaceState({}, "", "/app/capture?tag=rig-2");

    render(
      <CaptureHarness
        initialProjectId="project-1"
        projects={[project("project-1", "Project One"), project("project-2", "Project Two")]}
        sessions={[session({ projectId: "project-2", sessionId: "session-9" })]}
      />
    );

    await waitFor(() => expect(screen.getByLabelText("Project")).toHaveValue("project-2"));
    expect(screen.getByLabelText("Session (optional)")).toHaveValue("session-9");
    expect(screen.getByLabelText("Short hint (optional)")).toHaveValue("Bench 4");

    fireEvent.change(screen.getByLabelText("Message or hint"), {
      target: { value: "Fly loaded" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save capture" }));

    await waitFor(() => expect(noteBody).not.toBeNull());
    expect(noteBody.project_id).toBe("project-2");
    expect(noteBody.metadata).toEqual({
      capture_entry: "tag_tap",
      capture_hint: "Bench 4",
      capture_kind: "text",
      capture_mode: "text",
      capture_review_status: "pending_review",
      capture_source: "mobile_capture",
      tag_label: "Rig 2",
      tag_slug: "rig-2",
    });
  });

  it("flashes the unbound toast on a 404 tag and continues unchanged", async () => {
    const setFlash = vi.fn();
    installFetchMock([
      ...pendingCaptureRoutes("project-1"),
      {
        match: "/tags/missing",
        response: errorResponse("Tag binding not found.", 404),
      },
    ]);
    window.history.replaceState({}, "", "/app/capture?tag=missing");

    render(
      <CaptureHarness
        initialProjectId="project-1"
        projects={[project("project-1", "Project One")]}
        setFlash={setFlash}
      />
    );

    await waitFor(() => expect(setFlash).toHaveBeenCalledWith("", UNBOUND_TAG_MESSAGE));
    expect(screen.getByLabelText("Project")).toHaveValue("project-1");
    expect(screen.getByLabelText("Short hint (optional)")).toHaveValue("");
  });

  it("flashes the unbound toast for tag-status=unbound without extra fetches", async () => {
    const setFlash = vi.fn();
    const fetchMock = installFetchMock([...pendingCaptureRoutes("project-1")]);
    window.history.replaceState({}, "", "/app/capture?tag-status=unbound");

    render(
      <CaptureHarness
        initialProjectId="project-1"
        projects={[project("project-1", "Project One")]}
        setFlash={setFlash}
      />
    );

    await waitFor(() => expect(setFlash).toHaveBeenCalledWith("", UNBOUND_TAG_MESSAGE));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(4));
    expect(postNoteCalls(fetchMock)).toHaveLength(0);
  });

  it("lets explicit params override tag binding fields", async () => {
    installFetchMock([
      ...pendingCaptureRoutes("project-1"),
      ...pendingCaptureRoutes("project-3"),
      {
        match: "/tags/rig-2",
        response: apiResponse({
          hint: "Bench 4",
          label: "Rig 2",
          log_breadcrumb: false,
          mode: null,
          project_id: "project-2",
          session_id: null,
          slug: "rig-2",
        }),
      },
    ]);
    window.history.replaceState(
      {},
      "",
      "/app/capture?tag=rig-2&project=project-3&hint=Override"
    );

    render(
      <CaptureHarness
        initialProjectId="project-1"
        projects={[
          project("project-1", "Project One"),
          project("project-2", "Project Two"),
          project("project-3", "Project Three"),
        ]}
      />
    );

    await waitFor(() => expect(screen.getByLabelText("Project")).toHaveValue("project-3"));
    expect(screen.getByLabelText("Short hint (optional)")).toHaveValue("Override");
  });

  it("fires the log=1 breadcrumb once and dedupes the next mount", async () => {
    const breadcrumbBodies = [];
    const fetchMock = installFetchMock([
      ...pendingCaptureRoutes("project-1"),
      ...pendingCaptureRoutes("project-2"),
      {
        match: "/tags/rig-2",
        response: apiResponse({
          hint: null,
          label: "Rig 2",
          log_breadcrumb: false,
          mode: null,
          project_id: "project-2",
          session_id: "session-9",
          slug: "rig-2",
        }),
      },
      {
        match: "/notes",
        method: "POST",
        response: (request) => {
          breadcrumbBodies.push(JSON.parse(request.init.body));
          return apiResponse({ note_id: "note-42" }, 201);
        },
      },
    ]);
    window.history.replaceState({}, "", "/app/capture?tag=rig-2&log=1");
    const projects = [project("project-1", "Project One"), project("project-2", "Project Two")];

    const first = render(<CaptureHarness initialProjectId="project-1" projects={projects} />);

    await waitFor(() => expect(breadcrumbBodies).toHaveLength(1));
    expect(breadcrumbBodies[0]).toEqual({
      metadata: {
        capture_kind: "tag_breadcrumb",
        capture_review_status: "pending_review",
        capture_source: "tag_tap",
        tag_slug: "rig-2",
      },
      project_id: "project-2",
      raw_content: "Tag tap: Rig 2",
      targets: [{ entity_id: "session-9", entity_type: "session" }],
    });
    expect(sessionStorage.getItem("lab-tracker:tag-log:rig-2")).not.toBeNull();

    first.unmount();
    render(<CaptureHarness initialProjectId="project-1" projects={projects} />);
    await waitFor(() => expect(screen.getByLabelText("Project")).toHaveValue("project-2"));
    await settle();
    expect(postNoteCalls(fetchMock)).toHaveLength(1);
  });

  it("fires the breadcrumb when the binding sets log_breadcrumb without a log param", async () => {
    const breadcrumbBodies = [];
    installFetchMock([
      ...pendingCaptureRoutes("project-1"),
      ...pendingCaptureRoutes("project-2"),
      {
        match: "/tags/rig-2",
        response: apiResponse({
          hint: null,
          label: "Rig 2",
          log_breadcrumb: true,
          mode: null,
          project_id: "project-2",
          session_id: null,
          slug: "rig-2",
        }),
      },
      {
        match: "/notes",
        method: "POST",
        response: (request) => {
          breadcrumbBodies.push(JSON.parse(request.init.body));
          return apiResponse({ note_id: "note-42" }, 201);
        },
      },
    ]);
    window.history.replaceState({}, "", "/app/capture?tag=rig-2");

    render(
      <CaptureHarness
        initialProjectId="project-1"
        projects={[project("project-1", "Project One"), project("project-2", "Project Two")]}
      />
    );

    await waitFor(() => expect(breadcrumbBodies).toHaveLength(1));
    expect(breadcrumbBodies[0].raw_content).toBe("Tag tap: Rig 2");
    expect(breadcrumbBodies[0].targets).toEqual([]);
  });

  it("skips the breadcrumb without a token", async () => {
    const fetchMock = installFetchMock([...pendingCaptureRoutes("project-1")]);
    window.history.replaceState({}, "", "/app/capture?log=1");

    render(
      <CaptureHarness
        initialProjectId="project-1"
        projects={[project("project-1", "Project One")]}
        token=""
      />
    );

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(4));
    await settle();
    expect(postNoteCalls(fetchMock)).toHaveLength(0);
  });

  it("skips the breadcrumb without a resolved project", async () => {
    const fetchMock = installFetchMock([]);
    window.history.replaceState({}, "", "/app/capture?log=1");

    render(<CaptureHarness initialProjectId="" projects={[]} />);

    await settle();
    expect(fetchMock).not.toHaveBeenCalled();
  });
});

function recordedAudioFile(name = "bench.webm") {
  return new File(["voice"], name, { lastModified: 1718000000000, type: "audio/webm" });
}

describe("MobileCaptureCard voice-first capture", () => {
  it("renders the voice-first control and auto-uploads a recording", async () => {
    const uploadBodies = [];
    const setFlash = vi.fn();
    installFetchMock([
      ...pendingCaptureRoutes("project-1"),
      {
        match: "/notes/upload-file",
        method: "POST",
        response: (request) => {
          uploadBodies.push(request.init.body);
          return apiResponse({ note_id: "note-7" }, 201);
        },
      },
    ]);
    window.history.replaceState({}, "", "/app/capture?mode=voice");

    render(
      <CaptureHarness
        initialProjectId="project-1"
        projects={[project("project-1", "Project One")]}
        setFlash={setFlash}
      />
    );

    expect(await screen.findByText(VOICE_READY_STATUS)).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("Record voice note (voice-first)"), {
      target: { files: [recordedAudioFile()] },
    });

    await waitFor(() => expect(uploadBodies).toHaveLength(1));
    expect(uploadBodies[0].get("project_id")).toBe("project-1");
    expect(JSON.parse(uploadBodies[0].get("metadata"))).toEqual({
      capture_entry: "url_params",
      capture_kind: "voice",
      capture_mode: "voice",
      capture_review_status: "pending_review",
      capture_source: "mobile_capture",
      source_file_last_modified_at: new Date(1718000000000).toISOString(),
      source_file_last_modified_ms: 1718000000000,
      transcript_status: "pending",
      voice_note_type: "Observation",
    });
    await waitFor(() => expect(setFlash).toHaveBeenCalledWith("Capture saved for review."));
  });

  it("enters voice mode when the tag binding sets mode voice", async () => {
    installFetchMock([
      ...pendingCaptureRoutes("project-1"),
      ...pendingCaptureRoutes("project-2"),
      {
        match: "/tags/rig-2",
        response: apiResponse({
          hint: null,
          label: "Rig 2",
          log_breadcrumb: false,
          mode: "voice",
          project_id: "project-2",
          session_id: null,
          slug: "rig-2",
        }),
      },
    ]);
    window.history.replaceState({}, "", "/app/capture?tag=rig-2");

    render(
      <CaptureHarness
        initialProjectId="project-1"
        projects={[project("project-1", "Project One"), project("project-2", "Project Two")]}
      />
    );

    await waitFor(() => expect(screen.getByLabelText("Project")).toHaveValue("project-2"));
    expect(screen.getByText(VOICE_READY_STATUS)).toBeInTheDocument();
  });

  it("keeps a plain mount out of voice mode and never auto-uploads", async () => {
    const fetchMock = installFetchMock([...pendingCaptureRoutes("project-1")]);

    render(
      <CaptureHarness
        initialProjectId="project-1"
        projects={[project("project-1", "Project One")]}
      />
    );

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(4));
    expect(screen.queryByText(VOICE_READY_STATUS)).toBeNull();

    fireEvent.change(screen.getByLabelText("Record voice note"), {
      target: { files: [recordedAudioFile()] },
    });

    await settle();
    expect(requestedUrls(fetchMock)).not.toContain("/notes/upload-file");
    expect(screen.getByText("bench.webm")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save capture" })).toBeEnabled();
  });

  it("skips auto-upload when no project is selected", async () => {
    const fetchMock = installFetchMock([]);
    window.history.replaceState({}, "", "/app/capture?mode=voice");

    render(<CaptureHarness initialProjectId="" projects={[project("project-1", "Project One")]} />);

    expect(
      await screen.findByText("Choose a project below, then record to save.")
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Stay checked in" })).toBeDisabled();

    fireEvent.change(screen.getByLabelText("Record voice note (voice-first)"), {
      target: { files: [recordedAudioFile()] },
    });

    await settle();
    expect(fetchMock).not.toHaveBeenCalled();
    expect(screen.getByText("bench.webm")).toBeInTheDocument();
  });
});

describe("MobileCaptureCard bench check-in", () => {
  it("persists the merged tag context for checkin=1 with a ttl override", async () => {
    installFetchMock([
      ...pendingCaptureRoutes("project-1"),
      ...pendingCaptureRoutes("project-2"),
      {
        match: "/tags/rig-2",
        response: apiResponse({
          hint: "Bench 4",
          label: "Rig 2",
          log_breadcrumb: false,
          mode: null,
          project_id: "project-2",
          session_id: "session-9",
          slug: "rig-2",
        }),
      },
    ]);
    window.history.replaceState({}, "", "/app/capture?tag=rig-2&checkin=1&ttl-hours=4");
    const before = Date.now();

    render(
      <CaptureHarness
        initialProjectId="project-1"
        projects={[project("project-1", "Project One"), project("project-2", "Project Two")]}
        sessions={[session({ projectId: "project-2", sessionId: "session-9" })]}
      />
    );

    await waitFor(() => expect(screen.getByLabelText("Project")).toHaveValue("project-2"));
    expect(screen.getByLabelText("Session (optional)")).toHaveValue("session-9");
    const stored = JSON.parse(localStorage.getItem(BENCH_CHECKIN_KEY));
    expect(stored.projectId).toBe("project-2");
    expect(stored.sessionId).toBe("session-9");
    expect(stored.hint).toBe("Bench 4");
    expect(stored.label).toBe("Rig 2");
    expect(stored.expiresAt).toBeGreaterThan(before + 4 * HOUR_MS - 5000);
    expect(stored.expiresAt).toBeLessThanOrEqual(Date.now() + 4 * HOUR_MS);
    expect(screen.getByText(/Checked in: Rig 2 until/)).toBeInTheDocument();
  });

  it("clamps ttl-hours and falls back to the current selection without a tag", async () => {
    installFetchMock([...pendingCaptureRoutes("project-1")]);
    window.history.replaceState({}, "", "/app/capture?checkin=1&ttl-hours=99");

    render(
      <CaptureHarness
        initialProjectId="project-1"
        projects={[project("project-1", "Project One")]}
      />
    );

    await waitFor(() => expect(localStorage.getItem(BENCH_CHECKIN_KEY)).not.toBeNull());
    const stored = JSON.parse(localStorage.getItem(BENCH_CHECKIN_KEY));
    expect(stored.projectId).toBe("project-1");
    expect(stored.expiresAt).toBeGreaterThan(Date.now() + 23 * HOUR_MS);
    expect(stored.expiresAt).toBeLessThanOrEqual(Date.now() + 24 * HOUR_MS);
    expect(screen.getByText(/Checked in: Project One until/)).toBeInTheDocument();
  });

  it("applies an active check-in to project, session, and hint on a plain mount", async () => {
    localStorage.setItem(
      BENCH_CHECKIN_KEY,
      JSON.stringify({
        expiresAt: Date.now() + HOUR_MS,
        hint: "Bench 4",
        label: "Rig 2",
        projectId: "project-2",
        sessionId: "session-9",
      })
    );
    installFetchMock([...pendingCaptureRoutes("project-1"), ...pendingCaptureRoutes("project-2")]);

    render(
      <CaptureHarness
        initialProjectId="project-1"
        projects={[project("project-1", "Project One"), project("project-2", "Project Two")]}
        sessions={[session({ projectId: "project-2", sessionId: "session-9" })]}
      />
    );

    await waitFor(() => expect(screen.getByLabelText("Project")).toHaveValue("project-2"));
    expect(screen.getByLabelText("Session (optional)")).toHaveValue("session-9");
    expect(screen.getByLabelText("Short hint (optional)")).toHaveValue("Bench 4");
    expect(screen.getByText(/Checked in: Rig 2 until/)).toBeInTheDocument();
  });

  it("ignores and removes an expired check-in", async () => {
    localStorage.setItem(
      BENCH_CHECKIN_KEY,
      JSON.stringify({
        expiresAt: Date.now() - 1000,
        hint: "Bench 4",
        label: "Rig 2",
        projectId: "project-2",
        sessionId: "",
      })
    );
    const fetchMock = installFetchMock([...pendingCaptureRoutes("project-1")]);

    render(
      <CaptureHarness
        initialProjectId="project-1"
        projects={[project("project-1", "Project One")]}
      />
    );

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(4));
    expect(screen.getByLabelText("Project")).toHaveValue("project-1");
    expect(screen.getByLabelText("Short hint (optional)")).toHaveValue("");
    expect(localStorage.getItem(BENCH_CHECKIN_KEY)).toBeNull();
    expect(screen.queryByText(/Checked in:/)).toBeNull();
    expect(screen.getByRole("button", { name: "Stay checked in" })).toBeInTheDocument();
  });

  it("lets explicit URL params beat the check-in without clobbering it", async () => {
    localStorage.setItem(
      BENCH_CHECKIN_KEY,
      JSON.stringify({
        expiresAt: Date.now() + HOUR_MS,
        hint: "Bench 4",
        label: "Rig 2",
        projectId: "project-2",
        sessionId: "",
      })
    );
    installFetchMock([
      ...pendingCaptureRoutes("project-1"),
      ...pendingCaptureRoutes("project-2"),
      ...pendingCaptureRoutes("project-3"),
    ]);
    window.history.replaceState({}, "", "/app/capture?project=project-3&hint=Override");

    render(
      <CaptureHarness
        initialProjectId="project-1"
        projects={[
          project("project-1", "Project One"),
          project("project-2", "Project Two"),
          project("project-3", "Project Three"),
        ]}
      />
    );

    await waitFor(() => expect(screen.getByLabelText("Project")).toHaveValue("project-3"));
    expect(screen.getByLabelText("Short hint (optional)")).toHaveValue("Override");
    expect(JSON.parse(localStorage.getItem(BENCH_CHECKIN_KEY)).projectId).toBe("project-2");
  });

  it("supports manual check-in and check-out with zero tags", async () => {
    const fetchMock = installFetchMock([...pendingCaptureRoutes("project-1")]);

    render(
      <CaptureHarness
        initialProjectId="project-1"
        projects={[project("project-1", "Project One")]}
      />
    );

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(4));
    fireEvent.click(screen.getByRole("button", { name: "Stay checked in" }));

    const stored = JSON.parse(localStorage.getItem(BENCH_CHECKIN_KEY));
    expect(stored.projectId).toBe("project-1");
    expect(stored.label).toBe("");
    expect(stored.sessionId).toBe("");
    expect(stored.expiresAt).toBeGreaterThan(Date.now());
    expect(screen.getByText(/Checked in: Project One until/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Check out" }));
    expect(localStorage.getItem(BENCH_CHECKIN_KEY)).toBeNull();
    expect(screen.queryByText(/Checked in:/)).toBeNull();
    expect(screen.getByRole("button", { name: "Stay checked in" })).toBeInTheDocument();
  });

  it("leads the share migration effect to use the checked-in project", async () => {
    localStorage.setItem(
      BENCH_CHECKIN_KEY,
      JSON.stringify({
        expiresAt: Date.now() + HOUR_MS,
        hint: "",
        label: "Rig 2",
        projectId: "project-2",
        sessionId: "",
      })
    );
    shareMigration.queue.current = {
      drain: vi.fn(async () => ({ dropped: [] })),
      enqueue: vi.fn(),
    };
    installFetchMock([...pendingCaptureRoutes("project-1"), ...pendingCaptureRoutes("project-2")]);

    render(
      <CaptureHarness
        initialProjectId="project-1"
        projects={[project("project-1", "Project One"), project("project-2", "Project Two")]}
      />
    );

    await waitFor(() => expect(screen.getByLabelText("Project")).toHaveValue("project-2"));
    await waitFor(() => {
      const calls = shareMigration.migrate.mock.calls;
      expect(calls.length).toBeGreaterThan(0);
      // EVERY run must use the checked-in project — the first call is the
      // one that actually drains the inbox, so a stale mount-time project
      // there silently misfiles the shared captures.
      expect(calls[0][0].projectId).toBe("project-2");
      expect(calls.every(([options]) => options.projectId === "project-2")).toBe(true);
    });
  });

  it("migrates shares on a plain mount with the mount project", async () => {
    shareMigration.queue.current = {
      drain: vi.fn(async () => ({ dropped: [] })),
      enqueue: vi.fn(),
    };
    installFetchMock([...pendingCaptureRoutes("project-1")]);

    render(
      <CaptureHarness
        initialProjectId="project-1"
        projects={[project("project-1", "Project One")]}
      />
    );

    await waitFor(() => expect(shareMigration.migrate).toHaveBeenCalled());
    expect(shareMigration.migrate.mock.calls[0][0].projectId).toBe("project-1");
  });

  it("defers share migration until the tag context is applied", async () => {
    shareMigration.queue.current = {
      drain: vi.fn(async () => ({ dropped: [] })),
      enqueue: vi.fn(),
    };
    const tagResponse = deferred();
    installFetchMock([
      ...pendingCaptureRoutes("project-1"),
      ...pendingCaptureRoutes("project-2"),
      { match: "/tags/rig-2", response: () => tagResponse.promise },
    ]);
    window.history.replaceState({}, "", "/app/capture?tag=rig-2");

    render(
      <CaptureHarness
        initialProjectId="project-1"
        projects={[project("project-1", "Project One"), project("project-2", "Project Two")]}
      />
    );

    // While the tag binding is unresolved, no migration may run: it would
    // import shares into the stale mount-time project.
    await settle();
    expect(shareMigration.migrate).not.toHaveBeenCalled();

    tagResponse.resolve(apiResponse(tagBinding()));
    await waitFor(() => expect(shareMigration.migrate).toHaveBeenCalled());
    expect(
      shareMigration.migrate.mock.calls.every(([options]) => options.projectId === "project-2")
    ).toBe(true);
  });
});

describe("MobileCaptureCard URL-param effect cancellation", () => {
  it("never applies a slow tag binding after unmount", async () => {
    const tagResponse = deferred();
    installFetchMock([
      ...pendingCaptureRoutes("project-1"),
      { match: "/tags/rig-2", response: () => tagResponse.promise },
    ]);
    window.history.replaceState({}, "", "/app/capture?tag=rig-2");
    const onSelectedProjectChange = vi.fn();

    const view = renderRawCard({
      onSelectedProjectChange,
      projects: [project("project-1", "Project One"), project("project-2", "Project Two")],
      selectedProjectId: "project-1",
    });
    await settle();
    view.unmount();
    onSelectedProjectChange.mockClear();

    tagResponse.resolve(apiResponse(tagBinding()));
    await settle();
    // onSelectedProjectChange is an app-wide setter that outlives the card;
    // firing it here would flip the workspace project after navigation.
    expect(onSelectedProjectChange).not.toHaveBeenCalled();
  });

  it("never applies a slow session link after unmount", async () => {
    const sessionResponse = deferred();
    installFetchMock([
      ...pendingCaptureRoutes("project-1"),
      { match: "/sessions/by-link/ABC123", response: () => sessionResponse.promise },
    ]);
    window.history.replaceState({}, "", "/app/capture?session-link=ABC123");
    const onSelectedProjectChange = vi.fn();

    const view = renderRawCard({
      onSelectedProjectChange,
      projects: [project("project-1", "Project One"), project("project-2", "Project Two")],
      selectedProjectId: "project-1",
    });
    await settle();
    view.unmount();
    onSelectedProjectChange.mockClear();

    sessionResponse.resolve(
      apiResponse(session({ projectId: "project-2", sessionId: "session-9" }))
    );
    await settle();
    expect(onSelectedProjectChange).not.toHaveBeenCalled();
  });

  it("keeps hint text typed while the tag fetch is in flight", async () => {
    const tagResponse = deferred();
    installFetchMock([
      ...pendingCaptureRoutes("project-1"),
      ...pendingCaptureRoutes("project-2"),
      { match: "/tags/rig-2", response: () => tagResponse.promise },
    ]);
    window.history.replaceState({}, "", "/app/capture?tag=rig-2");

    render(
      <CaptureHarness
        initialProjectId="project-1"
        projects={[project("project-1", "Project One"), project("project-2", "Project Two")]}
      />
    );

    fireEvent.change(screen.getByLabelText("Short hint (optional)"), {
      target: { value: "typed mid-flight" },
    });
    tagResponse.resolve(apiResponse(tagBinding({ hint: "Bench 4" })));

    await waitFor(() => expect(screen.getByLabelText("Project")).toHaveValue("project-2"));
    expect(screen.getByLabelText("Short hint (optional)")).toHaveValue("typed mid-flight");
  });
});

describe("MobileCaptureCard upload re-entry guard", () => {
  it("blocks a second save while an upload is in flight", async () => {
    const noteResponse = deferred();
    const fetchMock = installFetchMock([
      ...pendingCaptureRoutes("project-1"),
      { match: "/notes", method: "POST", response: () => noteResponse.promise },
    ]);

    render(
      <CaptureHarness
        initialProjectId="project-1"
        projects={[project("project-1", "Project One")]}
      />
    );
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(4));

    fireEvent.change(screen.getByLabelText("Message or hint"), {
      target: { value: "Once only" },
    });
    const sendButton = screen.getByRole("button", { name: "Save capture" });
    fireEvent.click(sendButton);

    await waitFor(() => expect(postNoteCalls(fetchMock)).toHaveLength(1));
    expect(sendButton).toBeDisabled();
    expect(screen.getByRole("button", { name: "Save for later" })).toBeDisabled();
    fireEvent.click(sendButton);
    fireEvent.click(screen.getByRole("button", { name: "Save for later" }));

    noteResponse.resolve(apiResponse({ note_id: "note-1" }, 201));
    await settle();
    expect(postNoteCalls(fetchMock)).toHaveLength(1);
  });
});

describe("MobileCaptureCard voice-first arming boundaries", () => {
  it("keeps the shared mic and voice file picker manual in voice-first mode", async () => {
    const fetchMock = installFetchMock([...pendingCaptureRoutes("project-1")]);
    window.history.replaceState({}, "", "/app/capture?mode=voice");

    render(
      <CaptureHarness
        initialProjectId="project-1"
        projects={[project("project-1", "Project One")]}
      />
    );
    expect(await screen.findByText(VOICE_READY_STATUS)).toBeInTheDocument();

    // Composer mic (shared record input): stages, never auto-uploads.
    fireEvent.change(document.getElementById("capture-audio-record-input"), {
      target: { files: [recordedAudioFile("mic.webm")] },
    });
    await settle();
    expect(requestedUrls(fetchMock)).not.toContain("/notes/upload-file");
    expect(screen.getByText("mic.webm")).toBeInTheDocument();

    // Attachment-menu voice file picker: stages, never auto-uploads.
    fireEvent.click(screen.getByRole("button", { name: "Remove voice recording" }));
    fireEvent.change(screen.getByLabelText("Voice recording"), {
      target: { files: [recordedAudioFile("picked.webm")] },
    });
    await settle();
    expect(requestedUrls(fetchMock)).not.toContain("/notes/upload-file");
    expect(screen.getByText("picked.webm")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save capture" })).toBeEnabled();
  });
});

describe("MobileCaptureCard tag witness integrity", () => {
  it("strips the tag witness after a manual project change", async () => {
    let noteBody = null;
    installFetchMock([
      ...pendingCaptureRoutes("project-1"),
      ...pendingCaptureRoutes("project-2"),
      { match: "/tags/rig-2", response: apiResponse(tagBinding()) },
      {
        match: "/notes",
        method: "POST",
        response: (request) => {
          noteBody = JSON.parse(request.init.body);
          return apiResponse({ note_id: "note-99" }, 201);
        },
      },
    ]);
    window.history.replaceState({}, "", "/app/capture?tag=rig-2");

    render(
      <CaptureHarness
        initialProjectId="project-1"
        projects={[project("project-1", "Project One"), project("project-2", "Project Two")]}
      />
    );
    await waitFor(() => expect(screen.getByLabelText("Project")).toHaveValue("project-2"));

    fireEvent.change(screen.getByLabelText("Project"), { target: { value: "project-1" } });
    fireEvent.change(screen.getByLabelText("Message or hint"), {
      target: { value: "Re-aimed capture" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save capture" }));

    await waitFor(() => expect(noteBody).not.toBeNull());
    expect(noteBody.project_id).toBe("project-1");
    // The entry channel remains true; the physical-anchor witness does not.
    expect(noteBody.metadata.capture_entry).toBe("tag_tap");
    expect(noteBody.metadata.tag_slug).toBeUndefined();
    expect(noteBody.metadata.tag_label).toBeUndefined();
  });

  it("keeps the URL tag slug when the tag resolve fails transiently", async () => {
    let noteBody = null;
    const setFlash = vi.fn();
    const fetchMock = installFetchMock([
      ...pendingCaptureRoutes("project-1"),
      { match: "/tags/rig-2", response: errorResponse("upstream unavailable", 500) },
      {
        match: "/notes",
        method: "POST",
        response: (request) => {
          noteBody = JSON.parse(request.init.body);
          return apiResponse({ note_id: "note-99" }, 201);
        },
      },
    ]);
    window.history.replaceState({}, "", "/app/capture?tag=rig-2");

    render(
      <CaptureHarness
        initialProjectId="project-1"
        projects={[project("project-1", "Project One")]}
        setFlash={setFlash}
      />
    );
    await waitFor(() => expect(requestedUrls(fetchMock)).toContain("/tags/rig-2"));
    await settle();

    fireEvent.change(screen.getByLabelText("Message or hint"), {
      target: { value: "Network blip capture" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save capture" }));

    await waitFor(() => expect(noteBody).not.toBeNull());
    // A transient failure is not an unbound tag: the slug witness survives.
    expect(noteBody.metadata.tag_slug).toBe("rig-2");
    expect(noteBody.metadata.tag_label).toBeUndefined();
    expect(noteBody.metadata.capture_entry).toBe("tag_tap");
    expect(setFlash).not.toHaveBeenCalledWith("", UNBOUND_TAG_MESSAGE);
  });

  it("does not log a breadcrumb for a tagless log=1 URL", async () => {
    const fetchMock = installFetchMock([...pendingCaptureRoutes("project-1")]);
    window.history.replaceState({}, "", "/app/capture?log=1");

    render(
      <CaptureHarness
        initialProjectId="project-1"
        projects={[project("project-1", "Project One")]}
      />
    );

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(4));
    await settle();
    // No slug means no physical tag to witness: nothing to breadcrumb.
    expect(postNoteCalls(fetchMock)).toHaveLength(0);
  });

  it("never fires the breadcrumb against a project picked after the mount merge", async () => {
    const fetchMock = installFetchMock([
      ...pendingCaptureRoutes("project-1"),
      { match: "/tags/rig-2", response: errorResponse("upstream unavailable", 500) },
    ]);
    window.history.replaceState({}, "", "/app/capture?tag=rig-2&log=1");

    render(<CaptureHarness initialProjectId="" projects={[project("project-1", "Project One")]} />);
    await settle();

    // The mount merge resolved no project (binding failed, nothing stored),
    // so a later manual pick must not become the breadcrumb's target.
    fireEvent.change(screen.getByLabelText("Project"), { target: { value: "project-1" } });
    await settle();
    expect(postNoteCalls(fetchMock)).toHaveLength(0);
  });
});

describe("MobileCaptureCard session prefill visibility", () => {
  it("shows and clears a prefilled session that is not in the active list", async () => {
    localStorage.setItem(
      BENCH_CHECKIN_KEY,
      JSON.stringify({
        expiresAt: Date.now() + HOUR_MS,
        hint: "",
        label: "Rig 2",
        projectId: "project-2",
        sessionId: "session-old",
      })
    );
    installFetchMock([...pendingCaptureRoutes("project-1"), ...pendingCaptureRoutes("project-2")]);

    render(
      <CaptureHarness
        initialProjectId="project-1"
        projects={[project("project-1", "Project One"), project("project-2", "Project Two")]}
        sessions={[session({ projectId: "project-2", sessionId: "session-9" })]}
      />
    );

    await waitFor(() => expect(screen.getByLabelText("Project")).toHaveValue("project-2"));
    const sessionSelect = screen.getByLabelText("Session (optional)");
    expect(sessionSelect).toHaveValue("session-old");
    expect(
      screen.getByRole("option", { name: "Linked session (no longer active)" })
    ).toBeInTheDocument();

    fireEvent.change(sessionSelect, { target: { value: "" } });
    expect(sessionSelect).toHaveValue("");
    expect(screen.queryByRole("option", { name: "Linked session (no longer active)" })).toBeNull();
  });
});

describe("MobileCaptureCard control gating and input hygiene", () => {
  it("disables Stay checked in for read-only viewers", async () => {
    const fetchMock = installFetchMock([...pendingCaptureRoutes("project-1")]);

    render(
      <CaptureHarness
        canWrite={false}
        initialProjectId="project-1"
        projects={[project("project-1", "Project One")]}
      />
    );

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(4));
    expect(screen.getByRole("button", { name: "Stay checked in" })).toBeDisabled();
  });

  it("resets file inputs after handling so the same file can be re-selected", async () => {
    const fetchMock = installFetchMock([...pendingCaptureRoutes("project-1")]);

    render(
      <CaptureHarness
        initialProjectId="project-1"
        projects={[project("project-1", "Project One")]}
      />
    );
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(4));

    // Browsers keep the input's value after a pick and suppress the change
    // event for a same-file re-pick; simulate the retained value and assert
    // the handler cleared it.
    const audioInput = screen.getByLabelText("Voice recording");
    Object.defineProperty(audioInput, "value", {
      configurable: true,
      value: "C:\\fakepath\\take.webm",
      writable: true,
    });
    fireEvent.change(audioInput, { target: { files: [recordedAudioFile("take.webm")] } });
    expect(screen.getByText("take.webm")).toBeInTheDocument();
    expect(audioInput.value).toBe("");

    const photoInput = screen.getByLabelText("Photo file");
    Object.defineProperty(photoInput, "value", {
      configurable: true,
      value: "C:\\fakepath\\shot.png",
      writable: true,
    });
    fireEvent.change(photoInput, {
      target: { files: [new File(["img"], "shot.png", { type: "image/png" })] },
    });
    expect(screen.getByText("shot.png")).toBeInTheDocument();
    expect(photoInput.value).toBe("");
  });
});
