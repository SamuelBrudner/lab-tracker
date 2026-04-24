import * as React from "react";

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { vi } from "vitest";

import { SessionDetailCard } from "./sessions.jsx";
import { apiResponse, installFetchMock } from "../test/utils.js";

describe("SessionDetailCard", () => {
  it("loads paginated outputs and scoped linked notes", async () => {
    installFetchMock([
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
        match: "/sessions/session-1/outputs?limit=200&offset=0",
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
        canWrite={true}
        onCloseSession={vi.fn(async () => null)}
        onPromoteSession={vi.fn(async () => null)}
      />
    );

    expect(await screen.findByText("rig/output-1.bin")).toBeInTheDocument();
    expect(await screen.findByText("Session-linked note")).toBeInTheDocument();
  });

  it("calls the close handler with the session and project ids", async () => {
    const onCloseSession = vi.fn(async () => ({ status: "closed" }));

    installFetchMock([
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
        match: "/sessions/session-1/outputs?limit=200&offset=0",
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
        canWrite={true}
        onCloseSession={onCloseSession}
        onPromoteSession={vi.fn(async () => null)}
      />
    );

    fireEvent.click(await screen.findByRole("button", { name: "Close session" }));

    await waitFor(() => {
      expect(onCloseSession).toHaveBeenCalledWith("session-1", "project-1");
    });
  });

  it("calls the promote handler with the selected active question", async () => {
    const onPromoteSession = vi.fn(async () => ({ session_id: "session-1" }));

    installFetchMock([
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
        match: "/sessions/session-1/outputs?limit=200&offset=0",
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
        canWrite={true}
        onCloseSession={vi.fn(async () => null)}
        onPromoteSession={onPromoteSession}
      />
    );

    fireEvent.click(await screen.findByRole("button", { name: "Promote to scientific" }));

    await waitFor(() => {
      expect(onPromoteSession).toHaveBeenCalledWith("session-1", "question-1", "project-1");
    });
  });
});
