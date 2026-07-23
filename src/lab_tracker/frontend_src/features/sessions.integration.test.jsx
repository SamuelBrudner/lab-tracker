import * as React from "react";

import {
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";

import { App } from "../app-shell.jsx";

import { TOKEN_STORAGE_KEY } from "../shared/constants.js";

import { installFetchMock } from "../test/utils.js";

import {
  activeSessionsPath,
  apiResponse,
  committedAnalysesPath,
  datasetListPath,
  noteCountPath,
  paged,
  project,
  projectsPath,
  question,
  questionListPath,
  recentNotesPath,
  session,
  stagedAnalysesPath,
} from "../test/fixtures.js";

describe("App", () => {
  it("starts and closes a session from the home route with the active-session loader", async () => {
    localStorage.setItem(TOKEN_STORAGE_KEY, "token-session-create");

    installFetchMock([
      {
        match: "/auth/me",
        response: apiResponse({ role: "admin", username: "sam" }),
      },
      {
        match: projectsPath,
        response: apiResponse([project("project-1", "Project One")]),
      },
      {
        match: questionListPath("project-1"),
        response: paged([
          question({
            text: "Primary question",
            updatedAt: "2026-04-20T02:00:00Z",
          }),
        ]),
      },
      {
        match: datasetListPath("project-1"),
        response: paged([]),
      },
      {
        match: noteCountPath("project-1"),
        response: paged([], { limit: 1, offset: 0, total: 0 }),
      },
      {
        match: recentNotesPath("project-1"),
        response: paged([]),
      },
      {
        match: activeSessionsPath("project-1"),
        response: [
          paged([]),
          paged([session()]),
          paged([]),
        ],
      },
      {
        match: stagedAnalysesPath("project-1"),
        response: paged([]),
      },
      {
        match: committedAnalysesPath("project-1"),
        response: paged([], { limit: 1, offset: 0, total: 0 }),
      },
      {
        match: "/sessions",
        method: "POST",
        response: apiResponse(session(), 201),
      },
      {
        match: "/sessions/session-1",
        method: "PATCH",
        response: apiResponse({
          ...session(),
          ended_at: "2026-04-20T04:00:00Z",
          status: "closed",
        }),
      },
    ]);

    render(<App />);

    expect(await screen.findByRole("heading", { name: "Sessions" })).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.getByLabelText("Active project")).toHaveValue("project-1");
    });
    await waitFor(() => {
      expect(screen.getByLabelText("Primary question (required)")).toHaveValue("question-1");
    });

    fireEvent.click(screen.getByRole("button", { name: "Start session" }));

    expect(await screen.findByText("ABC123")).toBeInTheDocument();
    expect(await screen.findByText("Session started.")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Close session" }));

    expect(await screen.findByText("Session closed.")).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.queryByText("ABC123")).not.toBeInTheDocument();
    });
  });
});
