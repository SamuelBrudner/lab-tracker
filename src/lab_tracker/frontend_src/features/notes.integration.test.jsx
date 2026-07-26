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
  datasetCountPath,
  datasetListPath,
  note,
  noteCountPath,
  paged,
  project,
  projectsPath,
  question,
  questionCountPath,
  questionListPath,
  recentNotesPath,
  requestedUrls,
  stagedAnalysesPath,
} from "../test/fixtures.js";

describe("App", () => {
  it("uploads a note file from the home route and refreshes recent notes", async () => {
    localStorage.setItem(TOKEN_STORAGE_KEY, "token-note-upload");

    const fetchMock = installFetchMock([
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
        response: paged([question({ text: "Active question" })]),
      },
      {
        match: datasetListPath("project-1"),
        response: paged([]),
      },
      {
        match: noteCountPath("project-1"),
        response: [
          paged([], { limit: 1, offset: 0, total: 0 }),
          paged([question()], { limit: 1, offset: 0, total: 1 }),
        ],
      },
      {
        match: recentNotesPath("project-1"),
        response: [
          paged([], { limit: 5, offset: 0, total: 0 }),
          paged([note({ transcribedText: "Captured session note" })], {
            limit: 5,
            offset: 0,
            total: 1,
          }),
        ],
      },
      {
        match: activeSessionsPath("project-1"),
        response: paged([]),
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
        match: questionCountPath("project-1"),
        response: paged([question()], { limit: 1, offset: 0, total: 1 }),
      },
      {
        match: datasetCountPath("project-1"),
        response: paged([], { limit: 1, offset: 0, total: 0 }),
      },
      {
        match: "/notes/upload-file",
        method: "POST",
        response: apiResponse(
          note({ transcribedText: "Captured session note" }),
          201
        ),
      },
    ]);

    render(<App />);

    expect(await screen.findByRole("heading", { name: "Note Capture" })).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.getByLabelText("Active project")).toHaveValue("project-1");
    });

    const file = new File(["note-bytes"], "note.txt", { type: "text/plain" });
    fireEvent.change(screen.getByLabelText("Select file"), {
      target: { files: [file] },
    });
    fireEvent.change(screen.getByLabelText("Manual transcript (optional)"), {
      target: { value: "Captured session note" },
    });
    const uploadButton = screen.getByRole("button", { name: "Upload note file" });
    await waitFor(() => expect(uploadButton).toBeEnabled());
    fireEvent.click(uploadButton);

    expect(await screen.findByText("Note file uploaded.")).toBeInTheDocument();
    expect(await screen.findByText("Captured session note")).toBeInTheDocument();

    expect(requestedUrls(fetchMock).filter((url) => url === recentNotesPath("project-1"))).toHaveLength(2);
  });
});
