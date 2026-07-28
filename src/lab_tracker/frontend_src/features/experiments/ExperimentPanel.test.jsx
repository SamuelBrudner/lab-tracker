import * as React from "react";

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { vi } from "vitest";

import { buildApiPath } from "../../shared/api.js";
import { apiResponse, installFetchMock } from "../../test/utils.js";
import { ExperimentPanel } from "./ExperimentPanel.jsx";

describe("ExperimentPanel", () => {
  it("lists compact Experiments and creates one against an active question", async () => {
    const listPath = buildApiPath("/experiments", {
      project_id: "project-1",
      limit: 20,
      offset: 0,
    });
    const navigate = vi.fn();
    installFetchMock([
      {
        match: listPath,
        response: [
          apiResponse(
            [
              {
                experiment_id: "experiment-existing",
                name: "Existing run",
                project_id: "project-1",
                status: "active",
                updated_at: "2026-07-24T12:00:00Z",
              },
            ],
            200,
            { limit: 20, offset: 0, total: 1 }
          ),
          apiResponse(
            [
              {
                experiment_id: "experiment-new",
                name: "Odor panel run",
                project_id: "project-1",
                status: "active",
                updated_at: "2026-07-24T13:00:00Z",
              },
            ],
            200,
            { limit: 20, offset: 0, total: 1 }
          ),
        ],
      },
      {
        match: "/experiments",
        method: "POST",
        response: (request) => {
          expect(JSON.parse(request.init.body)).toEqual({
            description: "Ten trial blocks",
            name: "Odor panel run",
            primary_question_id: "question-1",
            project_id: "project-1",
          });
          return apiResponse({
            experiment_id: "experiment-new",
            name: "Odor panel run",
            primary_question_id: "question-1",
            project_id: "project-1",
            status: "active",
          });
        },
      },
    ]);

    render(
      <ExperimentPanel
        token="token-1"
        canWrite={true}
        selectedProjectId="project-1"
        questions={[
          {
            question_id: "question-1",
            status: "active",
            text: "Does adaptation change turning?",
          },
        ]}
        navigate={navigate}
      />
    );

    expect(await screen.findByText("Existing run")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Experiment name"), {
      target: { value: "Odor panel run" },
    });
    fireEvent.change(screen.getByLabelText("Description (optional)"), {
      target: { value: "Ten trial blocks" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Create Experiment" }));

    await waitFor(() =>
      expect(navigate).toHaveBeenCalledWith("/app/experiments/experiment-new")
    );
  });
});
