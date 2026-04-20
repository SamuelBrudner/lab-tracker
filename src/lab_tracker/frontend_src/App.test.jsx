import * as React from "react";

import { fireEvent, render, screen } from "@testing-library/react";

import { App } from "./app-shell.jsx";
import { TOKEN_STORAGE_KEY } from "./shared/constants.js";
import { apiResponse, errorResponse, installFetchMock } from "./test/utils.js";

describe("App", () => {
  it("restores a stored session and signs out", async () => {
    localStorage.setItem(TOKEN_STORAGE_KEY, "token-1");
    installFetchMock([
      {
        match: "/auth/me",
        response: apiResponse({ role: "admin", username: "sam" }),
      },
      {
        match: "/projects",
        response: apiResponse([]),
      },
    ]);

    render(<App />);

    expect(await screen.findByText("sam")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Sign out" }));

    expect(await screen.findByRole("heading", { name: "Sign In" })).toBeInTheDocument();
    expect(localStorage.getItem(TOKEN_STORAGE_KEY)).toBeNull();
  });

  it("renders a question detail route after auth restore", async () => {
    const questionId = "11111111-1111-4111-8111-111111111111";
    localStorage.setItem(TOKEN_STORAGE_KEY, "token-2");
    window.history.replaceState({}, "", `/app/questions/${questionId}`);

    installFetchMock([
      {
        match: "/auth/me",
        response: apiResponse({ role: "viewer", username: "sam" }),
      },
      {
        match: "/projects",
        response: apiResponse([]),
      },
      {
        match: `/questions/${questionId}`,
        response: apiResponse({
          created_at: "2026-04-20T00:00:00Z",
          project_id: "project-1",
          question_id: questionId,
          question_type: "descriptive",
          status: "active",
          text: "How stable is the rig today?",
          updated_at: "2026-04-20T01:00:00Z",
        }),
      },
    ]);

    render(<App />);

    expect(await screen.findByRole("heading", { name: "Question Detail" })).toBeInTheDocument();
    expect(await screen.findByText("How stable is the rig today?")).toBeInTheDocument();
  });

  it("shows a visible restore error when session bootstrap fails", async () => {
    localStorage.setItem(TOKEN_STORAGE_KEY, "token-3");
    installFetchMock([
      {
        match: "/auth/me",
        response: errorResponse("Session expired.", 401),
      },
      {
        match: "/projects",
        response: apiResponse([]),
      },
    ]);

    render(<App />);

    expect(await screen.findByText("Session expired.")).toBeInTheDocument();
    expect(await screen.findByRole("heading", { name: "Sign In" })).toBeInTheDocument();
  });
});
