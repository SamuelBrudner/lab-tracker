import * as React from "react";

import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { dataset } from "../../test/fixtures.js";

import { AnalysisPanel } from "./AnalysisPanel.jsx";

function analysisRecord(analysisId, status) {
  return {
    analysis_id: analysisId,
    code_version: "sha-1",
    dataset_ids: ["dataset-1"],
    environment_hash: null,
    executed_at: "2026-04-20T00:00:00Z",
    method_hash: "method-1",
    status,
  };
}

function renderPanel() {
  render(
    <AnalysisPanel
      canWrite={true}
      busy={false}
      loading={false}
      error=""
      selectedProjectId="project-1"
      datasets={[dataset({ status: "committed" })]}
      stagedAnalyses={[analysisRecord("analysis-staged", "staged")]}
      recentCommittedAnalyses={[analysisRecord("analysis-committed", "committed")]}
      visualizationStates={{}}
      analysisDatasetIds={[]}
      analysisCodeVersion=""
      analysisMethodHash=""
      analysisEnvironmentHash=""
      onAnalysisDatasetIdsChange={vi.fn()}
      onAnalysisCodeVersionChange={vi.fn()}
      onAnalysisMethodHashChange={vi.fn()}
      onAnalysisEnvironmentHashChange={vi.fn()}
      onCreateAnalysis={vi.fn((event) => event.preventDefault())}
      onCommitAnalysis={vi.fn()}
      onArchiveAnalysis={vi.fn()}
      onLoadVisualizations={vi.fn(async () => undefined)}
      navigate={vi.fn()}
    />
  );
}

describe("AnalysisPanel", () => {
  it("keeps the analysis staging form behind the Manual staging disclosure while Commit/Archive stay visible", () => {
    renderPanel();

    // Commit and Archive are the browser's only paths for staged analyses.
    expect(screen.getByRole("button", { name: "Commit analysis" })).toBeVisible();
    expect(screen.getByRole("button", { name: "Archive analysis" })).toBeVisible();
    expect(screen.getByRole("button", { name: "Stage analysis" })).not.toBeVisible();

    fireEvent.click(screen.getByText("Manual staging (advanced)"));

    expect(screen.getByRole("button", { name: "Stage analysis" })).toBeVisible();
  });
});
