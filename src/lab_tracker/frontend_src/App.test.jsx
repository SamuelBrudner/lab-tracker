import * as React from "react";

import { fireEvent, render, screen, waitFor } from "@testing-library/react";

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
        match: "/projects?limit=200&offset=0",
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
        match: "/projects?limit=200&offset=0",
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
        match: "/projects?limit=200&offset=0",
        response: apiResponse([]),
      },
    ]);

    render(<App />);

    expect(await screen.findByText("Session expired.")).toBeInTheDocument();
    expect(await screen.findByRole("heading", { name: "Sign In" })).toBeInTheDocument();
  });

  it("loads paginated project data and refreshes when the active project changes", async () => {
    localStorage.setItem(TOKEN_STORAGE_KEY, "token-4");

    const firstProjectQuestions = Array.from({ length: 205 }, (_, index) => ({
      project_id: "project-1",
      question_id: `question-1-${index}`,
      question_type: "descriptive",
      status: "staged",
      text: `Project One Question ${index}`,
    }));
    const secondProjectQuestions = Array.from({ length: 3 }, (_, index) => ({
      project_id: "project-2",
      question_id: `question-2-${index}`,
      question_type: "descriptive",
      status: "staged",
      text: `Project Two Question ${index}`,
    }));

    installFetchMock([
      {
        match: "/auth/me",
        response: apiResponse({ role: "admin", username: "sam" }),
      },
      {
        match: "/projects?limit=200&offset=0",
        response: apiResponse([
          { name: "Project One", project_id: "project-1" },
          { name: "Project Two", project_id: "project-2" },
        ]),
      },
      {
        match: "/questions?project_id=project-1&limit=200&offset=0",
        response: apiResponse(firstProjectQuestions.slice(0, 200), 200, {
          limit: 200,
          offset: 0,
          total: 205,
        }),
      },
      {
        match: "/questions?project_id=project-1&limit=200&offset=200",
        response: apiResponse(firstProjectQuestions.slice(200), 200, {
          limit: 200,
          offset: 200,
          total: 205,
        }),
      },
      {
        match: "/datasets?project_id=project-1&limit=200&offset=0",
        response: apiResponse([]),
      },
      {
        match: "/notes?project_id=project-1&limit=200&offset=0",
        response: apiResponse([]),
      },
      {
        match: "/sessions?project_id=project-1&limit=200&offset=0",
        response: apiResponse([]),
      },
      {
        match: "/analyses?project_id=project-1&limit=200&offset=0",
        response: apiResponse([]),
      },
      {
        match: "/visualizations?project_id=project-1&limit=200&offset=0",
        response: apiResponse([]),
      },
      {
        match: "/questions?project_id=project-2&limit=200&offset=0",
        response: apiResponse(secondProjectQuestions),
      },
      {
        match: "/datasets?project_id=project-2&limit=200&offset=0",
        response: apiResponse([]),
      },
      {
        match: "/notes?project_id=project-2&limit=200&offset=0",
        response: apiResponse([]),
      },
      {
        match: "/sessions?project_id=project-2&limit=200&offset=0",
        response: apiResponse([]),
      },
      {
        match: "/analyses?project_id=project-2&limit=200&offset=0",
        response: apiResponse([]),
      },
      {
        match: "/visualizations?project_id=project-2&limit=200&offset=0",
        response: apiResponse([]),
      },
    ]);

    render(<App />);

    expect((await screen.findAllByText("Project One Question 204")).length).toBeGreaterThan(0);

    fireEvent.change(screen.getByLabelText("Active project"), {
      target: { value: "project-2" },
    });

    expect((await screen.findAllByText("Project Two Question 2")).length).toBeGreaterThan(0);
  });

  it("ignores stale project data after switching the active project", async () => {
    localStorage.setItem(TOKEN_STORAGE_KEY, "token-5");

    let resolveProjectOneQuestions;

    installFetchMock([
      {
        match: "/auth/me",
        response: apiResponse({ role: "admin", username: "sam" }),
      },
      {
        match: "/projects?limit=200&offset=0",
        response: apiResponse([
          { name: "Project One", project_id: "project-1" },
          { name: "Project Two", project_id: "project-2" },
        ]),
      },
      {
        match: "/questions?project_id=project-1&limit=200&offset=0",
        response: () =>
          new Promise((resolve) => {
            resolveProjectOneQuestions = () =>
              resolve(
                apiResponse([
                  {
                    project_id: "project-1",
                    question_id: "question-1",
                    question_type: "descriptive",
                    status: "staged",
                    text: "Project One Question 0",
                  },
                ])
              );
          }),
      },
      {
        match: "/datasets?project_id=project-1&limit=200&offset=0",
        response: apiResponse([]),
      },
      {
        match: "/notes?project_id=project-1&limit=200&offset=0",
        response: apiResponse([]),
      },
      {
        match: "/sessions?project_id=project-1&limit=200&offset=0",
        response: apiResponse([]),
      },
      {
        match: "/analyses?project_id=project-1&limit=200&offset=0",
        response: apiResponse([]),
      },
      {
        match: "/visualizations?project_id=project-1&limit=200&offset=0",
        response: apiResponse([]),
      },
      {
        match: "/questions?project_id=project-2&limit=200&offset=0",
        response: apiResponse([
          {
            project_id: "project-2",
            question_id: "question-2",
            question_type: "descriptive",
            status: "staged",
            text: "Project Two Question 0",
          },
        ]),
      },
      {
        match: "/datasets?project_id=project-2&limit=200&offset=0",
        response: apiResponse([]),
      },
      {
        match: "/notes?project_id=project-2&limit=200&offset=0",
        response: apiResponse([]),
      },
      {
        match: "/sessions?project_id=project-2&limit=200&offset=0",
        response: apiResponse([]),
      },
      {
        match: "/analyses?project_id=project-2&limit=200&offset=0",
        response: apiResponse([]),
      },
      {
        match: "/visualizations?project_id=project-2&limit=200&offset=0",
        response: apiResponse([]),
      },
    ]);

    render(<App />);

    await waitFor(() => expect(typeof resolveProjectOneQuestions).toBe("function"));

    fireEvent.change(screen.getByLabelText("Active project"), {
      target: { value: "project-2" },
    });

    expect(
      await screen.findByText("Project Two Question 0", { selector: "strong" })
    ).toBeInTheDocument();

    resolveProjectOneQuestions();

    await waitFor(() => {
      expect(screen.queryByText("Project One Question 0")).not.toBeInTheDocument();
      expect(screen.getByText("Project Two Question 0", { selector: "strong" })).toBeInTheDocument();
    });
  });
});
