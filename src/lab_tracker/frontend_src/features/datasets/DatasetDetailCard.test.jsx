import * as React from "react";

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { apiResponse, installFetchMock } from "../../test/utils.js";
import { dataset, project } from "../../test/fixtures.js";

import { DatasetDetailCard } from "./DatasetDetailCard.jsx";

function renderCard(depth) {
  window.history.replaceState(
    depth ? { labTracker: { depth } } : null,
    "",
    "/app/datasets/dataset-1"
  );
  installFetchMock([
    { match: "/datasets/dataset-1", response: apiResponse(dataset({ status: "committed" })) },
  ]);
  const navigate = vi.fn();
  render(
    <DatasetDetailCard
      token="token-1"
      datasetId="dataset-1"
      projects={[project("project-1", "Project One")]}
      navigate={navigate}
      onSetActiveProject={vi.fn()}
    />
  );
  return navigate;
}

describe("DatasetDetailCard", () => {
  it("Back returns to the previous in-app page, else the dashboard", async () => {
    const back = vi.spyOn(window.history, "back").mockImplementation(() => {});

    const direct = renderCard(0);
    fireEvent.click(await screen.findByRole("button", { name: "Back" }));
    expect(direct).toHaveBeenCalledWith("/app");
    expect(back).not.toHaveBeenCalled();
    cleanup();

    const fromApp = renderCard(1);
    fireEvent.click(await screen.findByRole("button", { name: "Back" }));
    expect(back).toHaveBeenCalledTimes(1);
    expect(fromApp).not.toHaveBeenCalled();
  });
});
