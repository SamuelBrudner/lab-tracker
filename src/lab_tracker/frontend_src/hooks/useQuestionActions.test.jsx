import * as React from "react";

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { apiResponse, installFetchMock } from "../test/utils.js";
import { useQuestionActions } from "./useQuestionActions.js";

function Harness({ refreshProjectData, setFlash }) {
  const [questionText, setQuestionText] = React.useState("Does sleep matter?");
  const [questionHypothesis, setQuestionHypothesis] = React.useState("");
  const actions = useQuestionActions({
    canWrite: true,
    questionHypothesis,
    questionText,
    questionType: "descriptive",
    refreshProjectData,
    selectedProjectId: "project-1",
    setBusy: vi.fn(),
    setFlash,
    setQuestionHypothesis,
    setQuestionText,
    token: "token-1",
  });
  return (
    <form onSubmit={actions.handleCreateQuestion}>
      <span data-testid="question-text">{questionText}</span>
      <button type="submit">Create</button>
      <button type="button" onClick={() => actions.handleActivateQuestion("question-1")}>
        Activate
      </button>
    </form>
  );
}

describe("useQuestionActions", () => {
  it("reports a created question as staged when only the follow-up refresh fails", async () => {
    installFetchMock([
      {
        match: "/questions",
        method: "POST",
        response: apiResponse({ question_id: "question-new" }, 201),
      },
    ]);
    const setFlash = vi.fn();
    const refreshProjectData = vi.fn(async () => {
      throw new Error("Network unavailable");
    });

    render(<Harness refreshProjectData={refreshProjectData} setFlash={setFlash} />);
    fireEvent.click(screen.getByRole("button", { name: "Create" }));

    await waitFor(() =>
      expect(setFlash).toHaveBeenLastCalledWith(
        "",
        "Question staged, but the project view could not be refreshed: Network unavailable"
      )
    );
    expect(setFlash).not.toHaveBeenCalledWith("", "Network unavailable");
    expect(screen.getByTestId("question-text")).toHaveTextContent("");
  });

  it("keeps the typed question when the create request itself fails", async () => {
    installFetchMock([
      {
        match: "/questions",
        method: "POST",
        response: apiResponse({ error: { message: "nope" } }, 500),
      },
    ]);
    const setFlash = vi.fn();
    const refreshProjectData = vi.fn();

    render(<Harness refreshProjectData={refreshProjectData} setFlash={setFlash} />);
    fireEvent.click(screen.getByRole("button", { name: "Create" }));

    await waitFor(() => expect(setFlash).toHaveBeenCalledTimes(2));
    expect(setFlash.mock.calls[1][0]).toBe("");
    expect(refreshProjectData).not.toHaveBeenCalled();
    expect(screen.getByTestId("question-text")).toHaveTextContent("Does sleep matter?");
  });

  it("reports an activated question as activated when only the refresh fails", async () => {
    installFetchMock([
      {
        match: "/questions/question-1",
        method: "PATCH",
        response: apiResponse({ question_id: "question-1", status: "active" }),
      },
    ]);
    const setFlash = vi.fn();
    const refreshProjectData = vi.fn(async () => {
      throw new Error("Network unavailable");
    });

    render(<Harness refreshProjectData={refreshProjectData} setFlash={setFlash} />);
    fireEvent.click(screen.getByRole("button", { name: "Activate" }));

    await waitFor(() =>
      expect(setFlash).toHaveBeenLastCalledWith(
        "",
        "Question activated, but the project view could not be refreshed: Network unavailable"
      )
    );
  });
});
