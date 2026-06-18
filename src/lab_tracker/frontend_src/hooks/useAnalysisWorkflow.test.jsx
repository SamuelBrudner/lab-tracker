import * as React from "react";

import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";

import { buildApiPath } from "../shared/api.js";
import { useAnalysisWorkflow } from "./useAnalysisWorkflow.js";
import { apiResponse, installFetchMock } from "../test/utils.js";

function deferred() {
  let resolve;
  const promise = new Promise((nextResolve) => {
    resolve = nextResolve;
  });
  return { promise, resolve };
}

function visualization(analysisId, filePath) {
  return {
    analysis_id: analysisId,
    created_at: "2026-04-20T00:00:00Z",
    file_path: filePath,
    viz_id: `${analysisId}-viz`,
    viz_type: "figure",
  };
}

function analysis(analysisId) {
  return {
    analysis_id: analysisId,
    code_version: "v1",
    created_at: "2026-06-18T12:00:00Z",
    dataset_ids: ["dataset-1"],
    method_hash: "method",
    project_id: "project-1",
    status: "committed",
    updated_at: "2026-06-18T12:00:00Z",
  };
}

function noop() {}

function Harness() {
  const { loadVisualizations, visualizationStates } = useAnalysisWorkflow({
    canWrite: true,
    enabled: false,
    selectedProjectId: "project-1",
    setBusy: noop,
    setFlash: noop,
    token: "token-1",
  });
  const firstState = visualizationStates["analysis-1"] || {};
  const secondState = visualizationStates["analysis-2"] || {};

  return (
    <div>
      <button type="button" onClick={() => loadVisualizations("analysis-1")}>
        Load first
      </button>
      <button type="button" onClick={() => loadVisualizations("analysis-2")}>
        Load second
      </button>
      <p data-testid="first-loading">{String(Boolean(firstState.loading))}</p>
      <p data-testid="second-loading">{String(Boolean(secondState.loading))}</p>
      <p>{firstState.items?.[0]?.file_path || ""}</p>
      <p>{secondState.items?.[0]?.file_path || ""}</p>
    </div>
  );
}

function RefreshHarness() {
  const { recentCommittedAnalyses, stagedAnalyses } = useAnalysisWorkflow({
    canWrite: true,
    enabled: true,
    selectedProjectId: "project-1",
    setBusy: noop,
    setFlash: noop,
    token: "token-1",
  });

  return (
    <div>
      <p data-testid="staged-count">{stagedAnalyses.length}</p>
      <p data-testid="recent-committed">{recentCommittedAnalyses[0]?.analysis_id || ""}</p>
    </div>
  );
}

describe("useAnalysisWorkflow", () => {
  it("loads recent committed analyses with one bounded request", async () => {
    const committedPath = buildApiPath("/analyses", {
      project_id: "project-1",
      status: "committed",
      limit: 5,
      recent_first: true,
    });
    const oldPaginatedCommittedPath = buildApiPath("/analyses", {
      project_id: "project-1",
      status: "committed",
      limit: 200,
      offset: 0,
    });
    const fetchMock = installFetchMock([
      {
        match: buildApiPath("/analyses", {
          project_id: "project-1",
          status: "staged",
          limit: 200,
          offset: 0,
        }),
        response: apiResponse([], 200, { limit: 200, offset: 0, total: 0 }),
      },
      {
        match: committedPath,
        response: apiResponse([analysis("analysis-recent")], 200, {
          limit: 5,
          offset: 0,
          total: 300,
        }),
      },
    ]);

    render(<RefreshHarness />);

    expect(await screen.findByTestId("recent-committed")).toHaveTextContent(
      "analysis-recent"
    );
    expect(fetchMock).toHaveBeenCalledWith(
      committedPath,
      expect.objectContaining({ method: "GET" })
    );
    expect(fetchMock.mock.calls.map(([url]) => url)).not.toContain(
      oldPaginatedCommittedPath
    );
  });

  it("tracks concurrent visualization loads per analysis", async () => {
    const first = deferred();
    const second = deferred();
    installFetchMock([
      {
        match: "/visualizations?analysis_id=analysis-1&limit=200&offset=0",
        response: () => first.promise,
      },
      {
        match: "/visualizations?analysis_id=analysis-2&limit=200&offset=0",
        response: () => second.promise,
      },
    ]);

    render(<Harness />);

    fireEvent.click(screen.getByRole("button", { name: "Load first" }));
    fireEvent.click(screen.getByRole("button", { name: "Load second" }));

    expect(await screen.findByTestId("first-loading")).toHaveTextContent("true");
    expect(await screen.findByTestId("second-loading")).toHaveTextContent("true");

    await act(async () => {
      first.resolve(apiResponse([visualization("analysis-1", "viz/one.png")]));
    });

    await waitFor(() => {
      expect(screen.getByTestId("first-loading")).toHaveTextContent("false");
    });
    expect(await screen.findByText("viz/one.png")).toBeInTheDocument();

    await act(async () => {
      second.resolve(apiResponse([visualization("analysis-2", "viz/two.png")]));
    });

    await waitFor(() => {
      expect(screen.getByTestId("second-loading")).toHaveTextContent("false");
    });
    expect(await screen.findByText("viz/two.png")).toBeInTheDocument();
  });
});
