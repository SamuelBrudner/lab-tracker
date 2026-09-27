import * as React from "react";

import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { dataset, question } from "../../test/fixtures.js";

import { DatasetPanel } from "./DatasetPanel.jsx";

function renderPanel() {
  render(
    <DatasetPanel
      canWrite={true}
      busy={false}
      selectedProjectId="project-1"
      datasetPrimaryQuestionId=""
      onDatasetPrimaryQuestionIdChange={vi.fn()}
      datasetSecondaryRaw=""
      onDatasetSecondaryRawChange={vi.fn()}
      onCreateDataset={vi.fn((event) => event.preventDefault())}
      questions={[question()]}
      datasets={[dataset()]}
      onCommitDataset={vi.fn()}
      datasetFilesById={{}}
      onLoadDatasetFiles={vi.fn(async () => undefined)}
      onUploadDatasetFiles={vi.fn()}
      onDeleteDatasetFile={vi.fn()}
    />
  );
}

describe("DatasetPanel", () => {
  it("keeps the staging form behind the Manual staging disclosure while staged controls stay visible", () => {
    renderPanel();

    // The staged list is the browser's commit path for datasets: always shown.
    expect(screen.getByRole("button", { name: "Manage files" })).toBeVisible();
    expect(screen.getByRole("button", { name: "Stage dataset" })).not.toBeVisible();

    fireEvent.click(screen.getByText("Manual staging (advanced)"));

    expect(screen.getByRole("button", { name: "Stage dataset" })).toBeVisible();
  });
});
