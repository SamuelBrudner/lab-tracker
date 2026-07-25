import * as React from "react";

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { vi } from "vitest";

import { SessionDetailCard } from "./sessions.jsx";
import { apiResponse, installFetchMock } from "../test/utils.js";

describe("SessionDetailCard", () => {
  it("loads bounded legacy outputs only after expansion and loads scoped linked notes", async () => {
    const fetchMock = installFetchMock([
      {
        match: /\/projects\/project-1\/members/,
        response: apiResponse([{ role: "contributor", user_id: "user-1" }]),
      },
      {
        match: "/sessions/session-1",
        response: apiResponse({
          created_at: "2026-04-20T00:00:00Z",
          link_code: "ABC123",
          primary_question_id: "question-1",
          project_id: "project-1",
          session_id: "session-1",
          session_type: "scientific",
          started_at: "2026-04-20T01:00:00Z",
          status: "active",
          updated_at: "2026-04-20T01:00:00Z",
        }),
      },
      {
        match: "/questions?project_id=project-1&status=active&limit=200&offset=0",
        response: apiResponse([
          {
            project_id: "project-1",
            question_id: "question-1",
            status: "active",
            text: "Is the rig stable?",
          },
        ]),
      },
      {
        match: "/sessions/session-1/outputs?limit=100&offset=0",
        response: apiResponse([
          {
            checksum: "sha256-output",
            created_at: "2026-04-20T02:00:00Z",
            file_path: "rig/output-1.bin",
            output_id: "output-1",
            size_bytes: 512,
          },
        ]),
      },
      {
        match: "/sessions/session-1/experiments?limit=50&offset=0",
        response: apiResponse([]),
      },
      {
        match: "/sessions/session-1/collections?limit=20&offset=0",
        response: apiResponse([]),
      },
      {
        match:
          "/notes?project_id=project-1&target_entity_type=session&target_entity_id=session-1&limit=200&offset=0",
        response: apiResponse([
          {
            created_at: "2026-04-20T03:00:00Z",
            note_id: "note-1",
            raw_content: "Session-linked note",
            status: "committed",
            targets: [
              {
                entity_id: "session-1",
                entity_type: "session",
              },
            ],
            transcribed_text: "",
          },
        ]),
      },
    ]);

    render(
      <SessionDetailCard
        token="token-1"
        sessionId="session-1"
        projects={[{ name: "Project One", project_id: "project-1" }]}
        navigate={vi.fn()}
        onSetActiveProject={vi.fn()}
        user={{ role: "editor", user_id: "user-1" }}
        canWrite={true}
        onCloseSession={vi.fn(async () => null)}
        onPromoteSession={vi.fn(async () => null)}
      />
    );

    expect(await screen.findByText("Session-linked note")).toBeInTheDocument();
    expect(
      fetchMock.mock.calls.some(([input]) =>
        String(input).includes("/sessions/session-1/outputs")
      )
    ).toBe(false);

    fireEvent.click(screen.getByRole("button", { name: "Show legacy outputs" }));
    expect(await screen.findByText("rig/output-1.bin")).toBeInTheDocument();
  });

  it("calls the close handler with the session and project ids", async () => {
    const onCloseSession = vi.fn(async () => ({
      created_at: "2026-04-20T00:00:00Z",
      ended_at: "2026-04-20T04:00:00Z",
      link_code: "ABC123",
      primary_question_id: "question-1",
      project_id: "project-1",
      session_id: "session-1",
      session_type: "scientific",
      started_at: "2026-04-20T01:00:00Z",
      status: "closed",
      updated_at: "2026-04-20T04:00:00Z",
    }));

    installFetchMock([
      {
        match: /\/projects\/project-1\/members/,
        response: apiResponse([{ role: "contributor", user_id: "user-1" }]),
      },
      {
        match: "/sessions/session-1",
        response: apiResponse({
          created_at: "2026-04-20T00:00:00Z",
          link_code: "ABC123",
          primary_question_id: "question-1",
          project_id: "project-1",
          session_id: "session-1",
          session_type: "scientific",
          started_at: "2026-04-20T01:00:00Z",
          status: "active",
          updated_at: "2026-04-20T01:00:00Z",
        }),
      },
      {
        match: "/questions?project_id=project-1&status=active&limit=200&offset=0",
        response: apiResponse([
          {
            project_id: "project-1",
            question_id: "question-1",
            status: "active",
            text: "Is the rig stable?",
          },
        ]),
      },
      {
        match: "/sessions/session-1/outputs?limit=100&offset=0",
        response: apiResponse([]),
      },
      {
        match: "/sessions/session-1/experiments?limit=50&offset=0",
        response: apiResponse([]),
      },
      {
        match: "/sessions/session-1/collections?limit=20&offset=0",
        response: apiResponse([]),
      },
      {
        match:
          "/notes?project_id=project-1&target_entity_type=session&target_entity_id=session-1&limit=200&offset=0",
        response: apiResponse([]),
      },
      {
        match: "/questions/question-1",
        response: apiResponse({
          project_id: "project-1",
          question_id: "question-1",
          status: "active",
          text: "Is the rig stable?",
        }),
      },
    ]);

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

    const closeButton = await screen.findByRole("button", { name: "Close session" });
    // Write access now resolves from the session's project membership fetch, so
    // the button starts disabled; wait for it to enable before clicking.
    await waitFor(() => expect(closeButton).toBeEnabled());
    fireEvent.click(closeButton);

    await waitFor(() => {
      expect(onCloseSession).toHaveBeenCalledWith("session-1", "project-1");
    });
    expect(await screen.findByText("closed")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Close session" })).not.toBeInTheDocument();
  });

  it("calls the promote handler with the selected active question", async () => {
    const onPromoteSession = vi.fn(async () => ({
      created_at: "2026-04-20T00:00:00Z",
      link_code: "ABC123",
      primary_question_id: "question-1",
      project_id: "project-1",
      session_id: "session-1",
      session_type: "scientific",
      started_at: "2026-04-20T01:00:00Z",
      status: "active",
      updated_at: "2026-04-20T02:00:00Z",
    }));

    installFetchMock([
      {
        match: /\/projects\/project-1\/members/,
        response: apiResponse([{ role: "contributor", user_id: "user-1" }]),
      },
      {
        match: "/sessions/session-1",
        response: apiResponse({
          created_at: "2026-04-20T00:00:00Z",
          link_code: "ABC123",
          primary_question_id: null,
          project_id: "project-1",
          session_id: "session-1",
          session_type: "operational",
          started_at: "2026-04-20T01:00:00Z",
          status: "active",
          updated_at: "2026-04-20T01:00:00Z",
        }),
      },
      {
        match: "/questions?project_id=project-1&status=active&limit=200&offset=0",
        response: apiResponse([
          {
            created_at: "2026-04-20T02:00:00Z",
            project_id: "project-1",
            question_id: "question-1",
            status: "active",
            text: "Is the rig stable?",
          },
        ]),
      },
      {
        match: "/sessions/session-1/outputs?limit=100&offset=0",
        response: apiResponse([]),
      },
      {
        match: "/sessions/session-1/experiments?limit=50&offset=0",
        response: apiResponse([]),
      },
      {
        match: "/sessions/session-1/collections?limit=20&offset=0",
        response: apiResponse([]),
      },
      {
        match:
          "/notes?project_id=project-1&target_entity_type=session&target_entity_id=session-1&limit=200&offset=0",
        response: apiResponse([]),
      },
    ]);

    render(
      <SessionDetailCard
        token="token-1"
        sessionId="session-1"
        projects={[{ name: "Project One", project_id: "project-1" }]}
        navigate={vi.fn()}
        onSetActiveProject={vi.fn()}
        user={{ role: "editor", user_id: "user-1" }}
        canWrite={true}
        onCloseSession={vi.fn(async () => null)}
        onPromoteSession={onPromoteSession}
      />
    );

    const promoteButton = await screen.findByRole("button", { name: "Promote to scientific" });
    // Wait for the /questions fetch to populate the selection before clicking;
    // otherwise the button is still disabled and the click is a no-op (flaky under load).
    await waitFor(() => expect(promoteButton).toBeEnabled());
    fireEvent.click(promoteButton);

    await waitFor(() => {
      expect(onPromoteSession).toHaveBeenCalledWith("session-1", "question-1", "project-1");
    });
    expect(await screen.findByText("scientific")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Promote to scientific" })).not.toBeInTheDocument();
  });
});
