import * as React from "react";

import { fireEvent, render, screen } from "@testing-library/react";

import { App } from "../app-shell.jsx";

import { TOKEN_STORAGE_KEY } from "../shared/constants.js";

import { installFetchMock } from "../test/utils.js";

import {
  activeSessionsPath,
  apiResponse,
  committedAnalysesPath,
  dataset,
  datasetFilesPath,
  datasetListPath,
  noteCountPath,
  paged,
  project,
  projectsPath,
  question,
  questionListPath,
  recentNotesPath,
  requestedUrls,
  stagedAnalysesPath,
} from "../test/fixtures.js";

describe("App", () => {
  it("loads staged dataset files lazily from the home route", async () => {
    localStorage.setItem(TOKEN_STORAGE_KEY, "token-dataset-files");

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
        response: paged([question({ text: "Primary question" })]),
      },
      {
        match: datasetListPath("project-1"),
        response: paged([
          dataset({
            datasetId: "dataset-1",
            status: "staged",
          }),
        ]),
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
        match: datasetFilesPath("dataset-1"),
        response: paged([
          {
            checksum: "sha256-file",
            file_id: "file-1",
            path: "staged/file-1.bin",
            size_bytes: 512,
          },
        ]),
      },
    ]);

    render(<App />);

    await screen.findByRole("heading", { name: "Dataset Queue" });

    expect(requestedUrls(fetchMock)).not.toContain(datasetFilesPath("dataset-1"));

    fireEvent.click(await screen.findByRole("button", { name: "Manage files" }));

    expect(await screen.findByText("staged/file-1.bin")).toBeInTheDocument();
    expect(requestedUrls(fetchMock).filter((url) => url === datasetFilesPath("dataset-1"))).toHaveLength(1);

    fireEvent.click(screen.getByRole("button", { name: "Hide files" }));
    fireEvent.click(screen.getByRole("button", { name: "Manage files" }));

    await screen.findByText("staged/file-1.bin");
    expect(requestedUrls(fetchMock).filter((url) => url === datasetFilesPath("dataset-1"))).toHaveLength(1);
  });
});
