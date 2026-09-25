import * as React from "react";

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
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

describe("GoalDetailCard Back", () => {
  function goalPayload() {
    return {
      attributes: {},
      created_at: "2026-06-05T15:38:42Z",
      external_ref: null,
      goal_id: "goal-1",
      goal_type: "paper",
      links: [],
      project_id: "project-1",
      status: "planned",
      summary: "",
      target_date: null,
      title: "Browser preview manuscript",
      updated_at: "2026-06-05T15:38:43Z",
    };
  }

  function renderWithDepth(depth) {
    window.history.replaceState(depth ? { labTracker: { depth } } : null, "", "/app/goals/goal-1");
    installFetchMock([{ match: "/goals/goal-1", response: apiResponse(goalPayload()) }]);
    const navigate = vi.fn();
    render(<GoalDetailCard token="token-1" goalId="goal-1" navigate={navigate} />);
    return navigate;
  }

  it("Back uses in-app history with /app fallback", async () => {
    const back = vi.spyOn(window.history, "back").mockImplementation(() => {});

    const direct = renderWithDepth(0);
    fireEvent.click(await screen.findByRole("button", { name: "Back" }));
    expect(direct).toHaveBeenCalledWith("/app");
    expect(back).not.toHaveBeenCalled();
    cleanup();

    const fromApp = renderWithDepth(1);
    fireEvent.click(await screen.findByRole("button", { name: "Back" }));
    expect(back).toHaveBeenCalledTimes(1);
    expect(fromApp).not.toHaveBeenCalled();
  });
});
