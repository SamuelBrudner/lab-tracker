import * as React from "react";

import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";

import { App } from "../app-shell.jsx";

import { TOKEN_STORAGE_KEY } from "../shared/constants.js";

import { installFetchMock } from "../test/utils.js";

import {
  activeSessionsPath,
  analysis,
  apiResponse,
  committedAnalysesPath,
  dataset,
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
  visualization,
  visualizationsPath,
} from "../test/fixtures.js";

describe("App", () => {
  it("loads analysis visualizations lazily and keeps commit/archive actions on the home route", async () => {
    localStorage.setItem(TOKEN_STORAGE_KEY, "token-analysis-queue");

    const stagedAnalysis = analysis({
      analysisId: "analysis-staged",
      codeVersion: "sha-staged",
      status: "staged",
    });
    const committedAnalysis = analysis({
      analysisId: "analysis-committed",
      codeVersion: "sha-committed",
      status: "committed",
    });
    const committedAfterCommit = analysis({
      analysisId: "analysis-staged",
      codeVersion: "sha-staged",
      status: "committed",
      updatedAt: "2026-04-20T03:00:00Z",
      executedAt: "2026-04-20T03:00:00Z",
    });

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
            commitHash: "commit-1",
            status: "committed",
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
        response: [
          paged([stagedAnalysis]),
          paged([]),
          paged([]),
        ],
      },
      {
        match: committedAnalysesPath("project-1"),
        response: [
          paged([committedAnalysis]),
          paged([committedAfterCommit, committedAnalysis], { limit: 200, offset: 0, total: 2 }),
          paged([committedAfterCommit]),
        ],
      },
      {
        match: visualizationsPath("analysis-committed"),
        response: paged([
          visualization({
            analysisId: "analysis-committed",
            filePath: "viz/analysis-committed.png",
            vizType: "heatmap",
          }),
        ]),
      },
      {
        match: "/analyses/analysis-staged/commit",
        method: "POST",
        response: apiResponse(committedAfterCommit),
      },
      {
        match: "/analyses/analysis-committed",
        method: "PATCH",
        response: apiResponse({
          ...committedAnalysis,
          status: "archived",
        }),
      },
    ]);

    render(<App />);

    expect(await screen.findByText("analysis-staged")).toBeInTheDocument();
    expect(await screen.findByText("analysis-committed")).toBeInTheDocument();
    expect(requestedUrls(fetchMock)).not.toContain(visualizationsPath("analysis-committed"));
    expect(requestedUrls(fetchMock).some((url) => url.startsWith("/visualizations?project_id="))).toBe(false);

    fireEvent.click(screen.getByRole("button", { name: "Load visualizations" }));

    expect(await screen.findByText("viz/analysis-committed.png")).toBeInTheDocument();

    const commitButton = screen.getByRole("button", { name: "Commit analysis" });
    await waitFor(() => expect(commitButton).toBeEnabled());
    fireEvent.click(commitButton);

    expect(await screen.findByText("Analysis committed.")).toBeInTheDocument();
    expect(await screen.findByText("analysis-staged")).toBeInTheDocument();

    const committedRow = screen.getByText("analysis-committed").closest("article");
    const archiveButton = within(committedRow).getByRole("button", { name: "Archive analysis" });
    await waitFor(() => expect(archiveButton).toBeEnabled());
    fireEvent.click(archiveButton);

    expect(await screen.findByText("Analysis archived.")).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.queryByText("analysis-committed")).not.toBeInTheDocument();
    });
  });
});
