import * as React from "react";

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { vi } from "vitest";

import { buildApiPath } from "../../shared/api.js";
import { apiResponse, installFetchMock } from "../../test/utils.js";
import { ExperimentDetailCard } from "./ExperimentDetailCard.jsx";

describe("ExperimentDetailCard", () => {
  it("keeps candidate lists lazy and advances the lifecycle forward", async () => {
    const sessionMembersPath = buildApiPath("/experiments/experiment-1/sessions", {
      limit: 25,
      offset: 0,
    });
    const datasetMembersPath = buildApiPath("/experiments/experiment-1/datasets", {
      limit: 25,
      offset: 0,
    });
    const candidateSessionsPath = buildApiPath("/sessions", {
      project_id: "project-1",
      limit: 200,
      offset: 0,
    });
    const candidateDatasetsPath = buildApiPath("/datasets/summaries", {
      project_id: "project-1",
      limit: 200,
      offset: 0,
    });
    const fetchMock = installFetchMock([
      {
        match: "/experiments/experiment-1",
        response: apiResponse({
          created_at: "2026-07-24T12:00:00Z",
          description: "",
          experiment_id: "experiment-1",
          name: "Odor run",
          primary_question_id: "question-1",
          project_id: "project-1",
          status: "active",
        }),
      },
      {
        match: sessionMembersPath,
        response: apiResponse([], 200, { limit: 25, offset: 0, total: 0 }),
      },
      {
        match: datasetMembersPath,
        response: apiResponse([], 200, { limit: 25, offset: 0, total: 0 }),
      },
      {
        match: candidateSessionsPath,
        response: apiResponse([], 200, { limit: 200, offset: 0, total: 0 }),
      },
      {
        match: candidateDatasetsPath,
        response: apiResponse([], 200, { limit: 200, offset: 0, total: 0 }),
      },
      {
        match: "/experiments/experiment-1",
        method: "PATCH",
        response: (request) => {
          expect(JSON.parse(request.init.body)).toEqual({ status: "closed" });
          return apiResponse({
            created_at: "2026-07-24T12:00:00Z",
            description: "",
            experiment_id: "experiment-1",
            name: "Odor run",
            primary_question_id: "question-1",
            project_id: "project-1",
            status: "closed",
          });
        },
      },
    ]);

    render(
      <ExperimentDetailCard
        token="token-1"
        experimentId="experiment-1"
        projects={[{ name: "Project One", project_id: "project-1" }]}
        navigate={vi.fn()}
        onSetActiveProject={vi.fn()}
        canWrite={true}
      />
    );

    expect(await screen.findByDisplayValue("Odor run")).toBeInTheDocument();
    const urlsBeforeManage = fetchMock.mock.calls.map(([input]) =>
      typeof input === "string" ? input : input.url
    );
    expect(urlsBeforeManage).not.toContain(candidateSessionsPath);
    expect(urlsBeforeManage).not.toContain(candidateDatasetsPath);

    fireEvent.click(screen.getByRole("button", { name: "Manage memberships" }));
    await waitFor(() => {
      const urls = fetchMock.mock.calls.map(([input]) =>
        typeof input === "string" ? input : input.url
      );
      expect(urls).toContain(candidateSessionsPath);
      expect(urls).toContain(candidateDatasetsPath);
    });

    fireEvent.click(screen.getByRole("button", { name: "Close Experiment" }));
    expect(await screen.findByRole("button", { name: "Archive Experiment" })).toBeInTheDocument();
    expect(screen.getByText(/Closed Experiments accept Dataset results/)).toBeInTheDocument();
  });
});
