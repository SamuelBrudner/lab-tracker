import * as React from "react";

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { apiResponse, installFetchMock } from "../test/utils.js";
import { useDatasetWorkflow } from "./useDatasetWorkflow.js";

function Harness({ refreshProjectData, setFlash }) {
  const workflow = useDatasetWorkflow({
    canWrite: true,
    datasets: [],
    questions: [{ question_id: "question-1", status: "active" }],
    refreshProjectData,
    selectedProjectId: "project-1",
    setBusy: vi.fn(),
    setFlash,
    token: "token-1",
  });
  return (
    <form onSubmit={workflow.handleCreateDataset}>
      <button type="button" onClick={() => workflow.setDatasetPrimaryQuestionId("question-1")}>
        Pick question
      </button>
      <span data-testid="primary">{workflow.datasetPrimaryQuestionId}</span>
      <button type="submit">Create</button>
      <button type="button" onClick={() => workflow.handleCommitDataset("dataset-1")}>
        Commit
      </button>
    </form>
  );
}

describe("useDatasetWorkflow", () => {
  it("reports a created dataset as staged when only the follow-up refresh fails", async () => {
    installFetchMock([
      {
        match: "/datasets",
        method: "POST",
        response: apiResponse({ dataset_id: "dataset-new" }, 201),
      },
    ]);
    const setFlash = vi.fn();
    const refreshProjectData = vi.fn(async () => {
      throw new Error("Network unavailable");
    });

    render(<Harness refreshProjectData={refreshProjectData} setFlash={setFlash} />);
    fireEvent.click(screen.getByRole("button", { name: "Pick question" }));
    await waitFor(() => expect(screen.getByTestId("primary")).toHaveTextContent("question-1"));
    fireEvent.click(screen.getByRole("button", { name: "Create" }));

    await waitFor(() =>
      expect(setFlash).toHaveBeenLastCalledWith(
        "",
        "Dataset staged, but the project view could not be refreshed: Network unavailable"
      )
    );
  });

  it("reports a committed dataset as committed when only the refresh fails", async () => {
    installFetchMock([
      {
        match: "/datasets/dataset-1",
        method: "PATCH",
        response: apiResponse({ dataset_id: "dataset-1", status: "committed" }),
      },
    ]);
    const setFlash = vi.fn();
    const refreshProjectData = vi.fn(async () => {
      throw new Error("Network unavailable");
    });

    render(<Harness refreshProjectData={refreshProjectData} setFlash={setFlash} />);
    fireEvent.click(screen.getByRole("button", { name: "Commit" }));

    await waitFor(() =>
      expect(setFlash).toHaveBeenLastCalledWith(
        "",
        "Dataset committed, but the project view could not be refreshed: Network unavailable"
      )
    );
  });
});
