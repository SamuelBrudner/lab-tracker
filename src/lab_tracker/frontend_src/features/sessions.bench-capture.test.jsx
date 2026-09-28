import * as React from "react";

import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { SessionDetailCard, SessionPanel } from "./sessions.jsx";
import { apiResponse, installFetchMock } from "../test/utils.js";

const ACTIVE_SESSION = {
  created_at: "2026-04-20T00:00:00Z",
  link_code: "ABC123",
  primary_question_id: null,
  project_id: "project-1",
  session_id: "session-1",
  session_type: "operational",
  started_at: "2026-04-20T01:00:00Z",
  status: "active",
  updated_at: "2026-04-20T01:00:00Z",
};
const CLOSED_SESSION = {
  ...ACTIVE_SESSION,
  ended_at: "2026-04-20T04:00:00Z",
  status: "closed",
  updated_at: "2026-04-20T04:00:00Z",
};
const CAPTURE_URL = "http://lab.example/app/capture?project_id=project-1&session_id=session-1";

function sessionRoutes(session = ACTIVE_SESSION) {
  return [
    {
      match: /\/projects\/project-1\/members/,
      response: apiResponse([{ role: "contributor", user_id: "user-1" }]),
    },
    {
      match: "/sessions/session-1/capture-link",
      response: apiResponse({
        capture_qr_svg: '<svg xmlns="http://www.w3.org/2000/svg"></svg>',
        capture_url: CAPTURE_URL,
        project_id: "project-1",
        session_id: "session-1",
      }),
    },
    { match: "/sessions/session-1", response: apiResponse(session) },
    {
      match: "/questions?project_id=project-1&status=active&limit=200&offset=0",
      response: apiResponse([]),
    },
    { match: "/sessions/session-1/outputs?limit=200&offset=0", response: apiResponse([]) },
    {
      match:
        "/notes?project_id=project-1&target_entity_type=session&target_entity_id=session-1&limit=200&offset=0",
      response: apiResponse([]),
    },
  ];
}

function renderDetail({ onCloseSession = vi.fn(async () => CLOSED_SESSION) } = {}) {
  render(
    <SessionDetailCard
      token="token-1"
      sessionId="session-1"
      projects={[{ name: "Project One", project_id: "project-1" }]}
      navigate={vi.fn()}
      onSetActiveProject={vi.fn()}
      user={{ role: "editor", user_id: "user-1" }}
      canWrite={true}
      onCloseSession={onCloseSession}
      onPromoteSession={vi.fn(async () => null)}
    />
  );
  return { onCloseSession };
}

afterEach(() => {
  delete window.NDEFReader;
});

describe("SessionDetailCard bench capture", () => {
  it("offers a voice debrief right after closing, without holding the close back", async () => {
    installFetchMock(sessionRoutes());
    const { onCloseSession } = renderDetail();

    const closeButton = await screen.findByRole("button", { name: "Close session" });
    await waitFor(() => expect(closeButton).toBeEnabled());
    fireEvent.click(closeButton);

    const debrief = await screen.findByRole("region", {
      name: "Session closed. Record a quick debrief?",
    });
    expect(onCloseSession).toHaveBeenCalledWith("session-1", "project-1");
    // The session is already closed while the debrief is on offer.
    expect(screen.getByText("closed")).toBeInTheDocument();
    expect(within(debrief).getByText("What surprised you?")).toBeInTheDocument();

    fireEvent.click(within(debrief).getByRole("button", { name: "Skip" }));
    expect(screen.queryByRole("region", { name: /debrief/i })).not.toBeInTheDocument();
  });

  it("does not offer a debrief when closing failed", async () => {
    installFetchMock(sessionRoutes());
    renderDetail({ onCloseSession: vi.fn(async () => null) });

    const closeButton = await screen.findByRole("button", { name: "Close session" });
    await waitFor(() => expect(closeButton).toBeEnabled());
    fireEvent.click(closeButton);

    expect(await screen.findByText("Failed to close session.")).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: /debrief/i })).not.toBeInTheDocument();
  });

  it("opens the debrief any time from the Debrief button", async () => {
    installFetchMock(sessionRoutes(CLOSED_SESSION));
    renderDetail();

    fireEvent.click(await screen.findByRole("button", { name: "Debrief" }));

    expect(await screen.findByRole("region", { name: "Session debrief" })).toBeInTheDocument();
  });

  it("offers end-of-session photo import", async () => {
    installFetchMock(sessionRoutes(CLOSED_SESSION));
    renderDetail();

    expect(await screen.findByRole("button", { name: "Import photos" })).toBeInTheDocument();
    expect(screen.getByLabelText("Photos to import")).toHaveAttribute("multiple");
  });

  it("offers to write an NFC station tag for an active session", async () => {
    window.NDEFReader = class {
      async write() {}
    };
    installFetchMock(sessionRoutes());
    renderDetail();

    expect(await screen.findByRole("button", { name: "Write NFC tag" })).toBeInTheDocument();
    expect(screen.getByText(`${CAPTURE_URL}&capture_channel=nfc`)).toBeInTheDocument();
  });
});

describe("SessionPanel bench capture", () => {
  it("offers a debrief for a session closed from the list", async () => {
    const onCloseSession = vi.fn(async () => CLOSED_SESSION);
    render(
      <SessionPanel
        canWrite={true}
        busy={false}
        loading={false}
        error=""
        projects={[{ name: "Project One", project_id: "project-1" }]}
        selectedProjectId="project-1"
        onSelectedProjectChange={vi.fn()}
        sessionType="operational"
        onSessionTypeChange={vi.fn()}
        sessionPrimaryQuestionId=""
        onSessionPrimaryQuestionIdChange={vi.fn()}
        activeQuestions={[]}
        questions={[]}
        sessions={[ACTIVE_SESSION]}
        onCreateSession={vi.fn()}
        onCloseSession={onCloseSession}
        navigate={vi.fn()}
        token="token-1"
        ownerId="user-1"
      />
    );

    fireEvent.click(screen.getByRole("button", { name: "Close session" }));

    const debrief = await screen.findByRole("region", {
      name: "Session closed. Record a quick debrief?",
    });
    expect(onCloseSession).toHaveBeenCalledWith("session-1", "project-1");
    fireEvent.click(within(debrief).getByRole("button", { name: "Skip" }));
    expect(screen.queryByRole("region", { name: /debrief/i })).not.toBeInTheDocument();
  });
});
