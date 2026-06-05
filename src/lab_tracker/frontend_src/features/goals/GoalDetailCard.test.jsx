import * as React from "react";

import { render, screen } from "@testing-library/react";
import { vi } from "vitest";

import { apiResponse, installFetchMock } from "../../test/utils.js";
import { GoalDetailCard } from "./GoalDetailCard.jsx";

describe("GoalDetailCard", () => {
  it("loads the goal detail when auth is disabled and no token is present", async () => {
    installFetchMock([
      {
        match: "/goals/goal-1",
        response: apiResponse({
          attributes: {
            target_venue: "Neuron",
          },
          created_at: "2026-06-05T15:38:42Z",
          external_ref: null,
          goal_id: "goal-1",
          goal_type: "paper",
          links: [
            {
              created_at: "2026-06-05T15:38:43Z",
              created_by: "user-1",
              goal_id: "goal-1",
              link_id: "link-1",
              link_status: "candidate",
              relation: "candidate_figure",
              slot: "Figure 3",
              target: {
                entity_id: "question-1",
                entity_type: "question",
              },
            },
          ],
          project_id: "project-1",
          status: "planned",
          summary: "",
          target_date: null,
          title: "Browser preview manuscript",
          updated_at: "2026-06-05T15:38:43Z",
        }),
      },
    ]);

    render(<GoalDetailCard token="" goalId="goal-1" navigate={vi.fn()} />);

    expect(await screen.findByText("Browser preview manuscript")).toBeInTheDocument();
    expect(await screen.findByText("candidate_figure / Figure 3")).toBeInTheDocument();
    expect(await screen.findByText("candidate question:question-1")).toBeInTheDocument();
  });
});
