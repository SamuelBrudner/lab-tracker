import * as React from "react";

import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";

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

describe("useAnalysisWorkflow", () => {
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
