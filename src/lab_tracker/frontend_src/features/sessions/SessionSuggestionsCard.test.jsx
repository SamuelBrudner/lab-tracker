import * as React from "react";

import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { apiResponse, errorResponse, installFetchMock } from "../../test/utils.js";
import { DISMISSED_KEY_PREFIX, SessionSuggestionsCard } from "./SessionSuggestionsCard.jsx";

const SUGGESTIONS_PATH = "/projects/project-1/session-suggestions";

function suggestion(overrides = {}) {
  return {
    booking_instrument: null,
    booking_note_id: null,
    booking_summary: null,
    booking_uid: null,
    capture_count: 3,
    capture_note_ids: ["note-a", "note-b", "note-c"],
    detail: "3 captures that day are in no session and name none.",
    end_at: "2026-09-27T10:45:00Z",
    kind: "start_session_from_captures",
    local_date: "2026-09-27",
    session_id: null,
    start_at: "2026-09-27T10:00:00Z",
    suggestion_id: "start_session_from_captures:project-1:2026-09-27",
    title: "Record a session for 2026-09-27 10:00-10:45",
    ...overrides,
  };
}

const QUIET = suggestion({
  capture_count: 1,
  capture_note_ids: ["note-last"],
  detail: "Still open, but its last capture was 6 h ago (4 captures in all).",
  end_at: "2026-09-28T07:00:00Z",
  kind: "close_quiet_session",
  local_date: null,
  session_id: "session-1",
  start_at: null,
  suggestion_id: "close_quiet_session:session-1:note-last",
  title: "End the operational session LT-ABC at 07:00 on 2026-09-28",
});

function report(suggestions) {
  return apiResponse({
    generated_at: "2026-09-28T13:00:00Z",
    lookback_days: 14,
    min_captures_per_day: 3,
    project_id: "project-1",
    quiet_threshold_minutes: 240,
    suggestions,
    timezone: "UTC",
  });
}

function session(overrides = {}) {
  return apiResponse({
    link_code: "ABC",
    project_id: "project-1",
    session_id: "session-new",
    session_type: "operational",
    started_at: "2026-09-27T10:00:00Z",
    status: "active",
    ...overrides,
  });
}

function bodies(fetchMock, method, url) {
  return fetchMock.mock.calls
    .filter(([calledUrl, init = {}]) => calledUrl === url && (init.method || "GET") === method)
    .map(([, init]) => JSON.parse(init.body));
}

beforeEach(() => {
  globalThis.localStorage.clear();
});

describe("SessionSuggestionsCard", () => {
  it("renders nothing without suggestions or when the read fails", async () => {
    const fetchMock = installFetchMock([{ match: SUGGESTIONS_PATH, response: report([]) }]);
    const { container, rerender } = render(
      <SessionSuggestionsCard projectId="project-1" token="t" canWrite />
    );
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    expect(container).toBeEmptyDOMElement();

    installFetchMock([{ match: SUGGESTIONS_PATH, response: errorResponse("boom", 500) }]);
    rerender(<SessionSuggestionsCard projectId="project-1" token="t2" canWrite />);
    await waitFor(() => expect(globalThis.fetch).toHaveBeenCalled());
    expect(container).toBeEmptyDOMElement();
  });

  it("ends a quiet session at its last capture through the session update route", async () => {
    const onApplied = vi.fn();
    const fetchMock = installFetchMock([
      { match: SUGGESTIONS_PATH, response: [report([QUIET]), report([])] },
      {
        match: "/sessions/session-1",
        method: "PATCH",
        response: session({ session_id: "session-1", status: "closed" }),
      },
    ]);
    render(
      <SessionSuggestionsCard projectId="project-1" token="t" canWrite onApplied={onApplied} />
    );

    fireEvent.click(await screen.findByRole("button", { name: "Apply" }));

    expect(await screen.findByText("Session ended.")).toBeInTheDocument();
    expect(bodies(fetchMock, "PATCH", "/sessions/session-1")).toEqual([
      { ended_at: "2026-09-28T07:00:00Z", status: "closed" },
    ]);
    expect(onApplied).toHaveBeenCalledTimes(1);
    expect(screen.queryByText(QUIET.title)).not.toBeInTheDocument();
  });

  it("records a past capture day as a closed session spanning its captures", async () => {
    const fetchMock = installFetchMock([
      { match: SUGGESTIONS_PATH, response: [report([suggestion()]), report([])] },
      { match: "/sessions", method: "POST", response: session() },
      {
        match: "/sessions/session-new",
        method: "PATCH",
        response: session({ status: "closed" }),
      },
    ]);
    render(<SessionSuggestionsCard projectId="project-1" token="t" canWrite />);

    fireEvent.click(await screen.findByRole("button", { name: "Apply" }));

    expect(await screen.findByText("Session recorded.")).toBeInTheDocument();
    expect(bodies(fetchMock, "POST", "/sessions")).toEqual([
      {
        project_id: "project-1",
        session_type: "operational",
        started_at: "2026-09-27T10:00:00Z",
      },
    ]);
    expect(bodies(fetchMock, "PATCH", "/sessions/session-new")).toEqual([
      { ended_at: "2026-09-27T10:45:00Z", status: "closed" },
    ]);
    expect(fetchMock.mock.calls.some(([url]) => String(url).startsWith("/notes/"))).toBe(false);
  });

  it("leaves an ongoing booking's session open", async () => {
    const future = new Date(Date.now() + 60 * 60 * 1000).toISOString();
    const booking = suggestion({
      booking_instrument: "Confocal 2",
      booking_note_id: "note-booking",
      capture_note_ids: ["note-booking"],
      end_at: future,
      kind: "start_session_from_booking",
      suggestion_id: "start_session_from_booking:project-1:abc",
      title: "Start a session for the Confocal 2 booking",
    });
    const fetchMock = installFetchMock([
      { match: SUGGESTIONS_PATH, response: [report([booking]), report([])] },
      { match: "/sessions", method: "POST", response: session() },
    ]);
    render(<SessionSuggestionsCard projectId="project-1" token="t" canWrite />);

    fireEvent.click(await screen.findByRole("button", { name: "Apply" }));

    expect(await screen.findByText("Session recorded.")).toBeInTheDocument();
    expect(bodies(fetchMock, "POST", "/sessions")).toHaveLength(1);
    expect(bodies(fetchMock, "PATCH", "/sessions/session-new")).toEqual([]);
  });

  it("attaches the listed captures only on the attach click, keeping their targets", async () => {
    const one = suggestion({ capture_note_ids: ["note-a"], capture_count: 1 });
    const fetchMock = installFetchMock([
      { match: SUGGESTIONS_PATH, response: [report([one]), report([])] },
      { match: "/sessions", method: "POST", response: session() },
      { match: "/sessions/session-new", method: "PATCH", response: session() },
      {
        match: "/notes/note-a",
        response: apiResponse({
          note_id: "note-a",
          project_id: "project-1",
          raw_content: "Added buffer",
          targets: [{ entity_id: "question-1", entity_type: "question" }],
        }),
      },
      {
        match: "/notes/note-a",
        method: "PATCH",
        response: apiResponse({ note_id: "note-a", project_id: "project-1" }),
      },
    ]);
    render(<SessionSuggestionsCard projectId="project-1" token="t" canWrite />);

    fireEvent.click(await screen.findByRole("button", { name: "Apply and attach 1 capture" }));

    expect(await screen.findByText("Session recorded and captures attached.")).toBeInTheDocument();
    expect(bodies(fetchMock, "PATCH", "/notes/note-a")).toEqual([
      {
        targets: [
          { entity_id: "question-1", entity_type: "question" },
          { entity_id: "session-new", entity_type: "session" },
        ],
      },
    ]);
  });

  it("keeps Apply for contributors but lets anyone dismiss", async () => {
    installFetchMock([{ match: SUGGESTIONS_PATH, response: report([suggestion()]) }]);
    render(<SessionSuggestionsCard projectId="project-1" token="t" canWrite={false} />);

    const region = await screen.findByRole("region", { name: "Session suggestions" });
    expect(within(region).getByRole("button", { name: "Apply" })).toBeDisabled();
    expect(within(region).getByRole("button", { name: "Apply and attach 3 captures" })).toBeDisabled();
    expect(within(region).getByRole("button", { name: "Dismiss" })).toBeEnabled();
  });

  it("remembers a dismissal on this device by suggestion id", async () => {
    installFetchMock([{ match: SUGGESTIONS_PATH, response: report([suggestion(), QUIET]) }]);
    const { unmount } = render(<SessionSuggestionsCard projectId="project-1" token="t" canWrite />);

    await screen.findByText(QUIET.title);
    const item = screen.getByText(suggestion().title).closest("li");
    fireEvent.click(within(item).getByRole("button", { name: "Dismiss" }));

    expect(screen.queryByText(suggestion().title)).not.toBeInTheDocument();
    expect(
      globalThis.localStorage.getItem(`${DISMISSED_KEY_PREFIX}${suggestion().suggestion_id}`)
    ).not.toBeNull();
    unmount();

    installFetchMock([{ match: SUGGESTIONS_PATH, response: report([suggestion(), QUIET]) }]);
    render(<SessionSuggestionsCard projectId="project-1" token="t" canWrite />);
    expect(await screen.findByText(QUIET.title)).toBeInTheDocument();
    expect(screen.queryByText(suggestion().title)).not.toBeInTheDocument();
  });

  it("tolerates unavailable storage when reading and dismissing", async () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("storage disabled");
    });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("quota exceeded");
    });
    installFetchMock([{ match: SUGGESTIONS_PATH, response: report([suggestion()]) }]);
    const { container } = render(
      <SessionSuggestionsCard projectId="project-1" token="t" canWrite />
    );

    fireEvent.click(await screen.findByRole("button", { name: "Dismiss" }));

    await waitFor(() => expect(container).toBeEmptyDOMElement());
  });

  it("shows the server's refusal and keeps the suggestion", async () => {
    installFetchMock([
      { match: SUGGESTIONS_PATH, response: report([QUIET]) },
      {
        match: "/sessions/session-1",
        method: "PATCH",
        response: errorResponse("Project contributor access required.", 403),
      },
    ]);
    render(<SessionSuggestionsCard projectId="project-1" token="t" canWrite />);

    fireEvent.click(await screen.findByRole("button", { name: "Apply" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Project contributor access required."
    );
    expect(screen.getByText(QUIET.title)).toBeInTheDocument();
  });
});
