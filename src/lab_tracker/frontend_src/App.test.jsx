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

  it("loads project summary counts instead of full workspace data on detail routes", async () => {
    const questionId = "11111111-1111-4111-8111-111111111111";
    localStorage.setItem(TOKEN_STORAGE_KEY, "token-detail-summary");
    window.history.replaceState({}, "", `/app/questions/${questionId}`);

    installFetchMock([
      {
        match: "/auth/me",
        response: apiResponse({ role: "viewer", username: "sam" }),
      },
      {
        match: "/projects?limit=200&offset=0",
        response: apiResponse([{ name: "Project One", project_id: "project-1" }]),
      },
      {
        match: "/questions?project_id=project-1&limit=1&offset=0",
        response: apiResponse(
          [
            {
              project_id: "project-1",
              question_id: "question-1",
              question_type: "descriptive",
              status: "active",
              text: "Project count placeholder",
            },
          ],
          200,
          { limit: 1, offset: 0, total: 12 }
        ),
      },
      {
        match: "/datasets?project_id=project-1&limit=1&offset=0",
        response: apiResponse([], 200, { limit: 1, offset: 0, total: 4 }),
      },
      {
        match: "/notes?project_id=project-1&limit=1&offset=0",
        response: apiResponse([], 200, { limit: 1, offset: 0, total: 7 }),
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
    expect(await screen.findByText("12")).toBeInTheDocument();
    expect(await screen.findByText("4")).toBeInTheDocument();
    expect(await screen.findByText("7")).toBeInTheDocument();
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

  it("creates a project from the home route", async () => {
    localStorage.setItem(TOKEN_STORAGE_KEY, "token-create-project");

    installFetchMock([
      {
        match: "/auth/me",
        response: apiResponse({ role: "admin", username: "sam" }),
      },
      {
        match: "/projects?limit=200&offset=0",
        response: [
          apiResponse([]),
          apiResponse([{ name: "Project One", project_id: "project-1" }]),
        ],
      },
      {
        match: "/projects",
        method: "POST",
        response: apiResponse({ name: "Project One", project_id: "project-1" }, 201),
      },
      {
        match: "/questions?project_id=project-1&limit=200&offset=0",
        response: apiResponse([]),
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
    ]);

    render(<App />);

    expect(await screen.findByRole("heading", { name: "Dashboard" })).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("Name"), {
      target: { value: "Project One" },
    });
    fireEvent.change(screen.getByLabelText("Description"), {
      target: { value: "Created from test" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Create project" }));

    expect(await screen.findByText("Project created.")).toBeInTheDocument();
    expect(await screen.findAllByRole("option", { name: "Project One" })).toHaveLength(2);
  });

  it("stages and activates a question from the home route", async () => {
    localStorage.setItem(TOKEN_STORAGE_KEY, "token-question-actions");

    installFetchMock([
      {
        match: "/auth/me",
        response: apiResponse({ role: "admin", username: "sam" }),
      },
      {
        match: "/projects?limit=200&offset=0",
        response: apiResponse([{ name: "Project One", project_id: "project-1" }]),
      },
      {
        match: "/questions?project_id=project-1&limit=200&offset=0",
        response: [
          apiResponse([]),
          apiResponse([
            {
              project_id: "project-1",
              question_id: "question-1",
              question_type: "descriptive",
              status: "staged",
              text: "How stable is the rig?",
            },
          ]),
          apiResponse([
            {
              project_id: "project-1",
              question_id: "question-1",
              question_type: "descriptive",
              status: "active",
              text: "How stable is the rig?",
            },
          ]),
        ],
      },
      {
        match: "/questions",
        method: "POST",
        response: apiResponse(
          {
            project_id: "project-1",
            question_id: "question-1",
            question_type: "descriptive",
            status: "staged",
            text: "How stable is the rig?",
          },
          201
        ),
      },
      {
        match: "/questions/question-1",
        method: "PATCH",
        response: apiResponse({
          project_id: "project-1",
          question_id: "question-1",
          question_type: "descriptive",
          status: "active",
          text: "How stable is the rig?",
        }),
      },
      {
        match: "/datasets?project_id=project-1&limit=200&offset=0",
        response: [apiResponse([]), apiResponse([]), apiResponse([])],
      },
      {
        match: "/notes?project_id=project-1&limit=200&offset=0",
        response: [apiResponse([]), apiResponse([]), apiResponse([])],
      },
      {
        match: "/sessions?project_id=project-1&limit=200&offset=0",
        response: [apiResponse([]), apiResponse([]), apiResponse([])],
      },
      {
        match: "/analyses?project_id=project-1&limit=200&offset=0",
        response: [apiResponse([]), apiResponse([]), apiResponse([])],
      },
      {
        match: "/visualizations?project_id=project-1&limit=200&offset=0",
        response: [apiResponse([]), apiResponse([]), apiResponse([])],
      },
    ]);

    render(<App />);

    expect(await screen.findByRole("heading", { name: "Question Staging & Commit" })).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("Question text"), {
      target: { value: "How stable is the rig?" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Stage question" }));

    expect(await screen.findByText("Question staged.")).toBeInTheDocument();
    expect(
      await screen.findByText("How stable is the rig?", { selector: ".item strong" })
    ).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Commit (activate)" }));

    expect(await screen.findByText("Question activated.")).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.queryByRole("button", { name: "Commit (activate)" })).not.toBeInTheDocument();
    });
  });

  it("uploads a note file from the home route", async () => {
    localStorage.setItem(TOKEN_STORAGE_KEY, "token-note-upload");

    installFetchMock([
      {
        match: "/auth/me",
        response: apiResponse({ role: "admin", username: "sam" }),
      },
      {
        match: "/projects?limit=200&offset=0",
        response: apiResponse([{ name: "Project One", project_id: "project-1" }]),
      },
      {
        match: "/questions?project_id=project-1&limit=200&offset=0",
        response: [
          apiResponse([
            {
              project_id: "project-1",
              question_id: "question-1",
              question_type: "descriptive",
              status: "active",
              text: "Active question",
            },
          ]),
          apiResponse([
            {
              project_id: "project-1",
              question_id: "question-1",
              question_type: "descriptive",
              status: "active",
              text: "Active question",
            },
          ]),
        ],
      },
      {
        match: "/notes/upload-file",
        method: "POST",
        response: apiResponse(
          {
            note_id: "note-1",
            project_id: "project-1",
            raw_content: "",
            status: "staged",
            transcribed_text: "Captured session note",
          },
          201
        ),
      },
      {
        match: "/notes?project_id=project-1&limit=200&offset=0",
        response: [
          apiResponse([]),
          apiResponse([
            {
              created_at: "2026-04-20T00:00:00Z",
              note_id: "note-1",
              project_id: "project-1",
              raw_content: "",
              status: "staged",
              transcribed_text: "Captured session note",
            },
          ]),
        ],
      },
      {
        match: "/datasets?project_id=project-1&limit=200&offset=0",
        response: [apiResponse([]), apiResponse([])],
      },
      {
        match: "/sessions?project_id=project-1&limit=200&offset=0",
        response: [apiResponse([]), apiResponse([])],
      },
      {
        match: "/analyses?project_id=project-1&limit=200&offset=0",
        response: [apiResponse([]), apiResponse([])],
      },
      {
        match: "/visualizations?project_id=project-1&limit=200&offset=0",
        response: [apiResponse([]), apiResponse([])],
      },
    ]);

    render(<App />);

    expect(await screen.findByRole("heading", { name: "Note Capture" })).toBeInTheDocument();

    const file = new File(["note-bytes"], "note.txt", { type: "text/plain" });
    fireEvent.change(screen.getByLabelText("Select file"), {
      target: { files: [file] },
    });
    fireEvent.change(screen.getByLabelText("Manual transcript (optional)"), {
      target: { value: "Captured session note" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Upload note file" }));

    expect(await screen.findByText("Note file uploaded.")).toBeInTheDocument();
    expect(await screen.findByText("Captured session note")).toBeInTheDocument();
  });

  it("starts a session from the home route", async () => {
    localStorage.setItem(TOKEN_STORAGE_KEY, "token-session-create");

    installFetchMock([
      {
        match: "/auth/me",
        response: apiResponse({ role: "admin", username: "sam" }),
      },
      {
        match: "/projects?limit=200&offset=0",
        response: apiResponse([{ name: "Project One", project_id: "project-1" }]),
      },
      {
        match: "/questions?project_id=project-1&limit=200&offset=0",
        response: [
          apiResponse([
            {
              project_id: "project-1",
              question_id: "question-1",
              question_type: "descriptive",
              status: "active",
              text: "Primary question",
              updated_at: "2026-04-20T02:00:00Z",
            },
          ]),
          apiResponse([
            {
              project_id: "project-1",
              question_id: "question-1",
              question_type: "descriptive",
              status: "active",
              text: "Primary question",
              updated_at: "2026-04-20T02:00:00Z",
            },
          ]),
        ],
      },
      {
        match: "/sessions",
        method: "POST",
        response: apiResponse(
          {
            link_code: "ABC123",
            primary_question_id: "question-1",
            project_id: "project-1",
            session_id: "session-1",
            session_type: "scientific",
            started_at: "2026-04-20T03:00:00Z",
            status: "active",
          },
          201
        ),
      },
      {
        match: "/sessions?project_id=project-1&limit=200&offset=0",
        response: [
          apiResponse([]),
          apiResponse([
            {
              link_code: "ABC123",
              primary_question_id: "question-1",
              project_id: "project-1",
              session_id: "session-1",
              session_type: "scientific",
              started_at: "2026-04-20T03:00:00Z",
              status: "active",
            },
          ]),
        ],
      },
      {
        match: "/datasets?project_id=project-1&limit=200&offset=0",
        response: [apiResponse([]), apiResponse([])],
      },
      {
        match: "/notes?project_id=project-1&limit=200&offset=0",
        response: [apiResponse([]), apiResponse([])],
      },
      {
        match: "/analyses?project_id=project-1&limit=200&offset=0",
        response: [apiResponse([]), apiResponse([])],
      },
      {
        match: "/visualizations?project_id=project-1&limit=200&offset=0",
        response: [apiResponse([]), apiResponse([])],
      },
    ]);

    render(<App />);

    expect(await screen.findByRole("heading", { name: "Sessions" })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Start session" }));

    expect(await screen.findByText("Session started.")).toBeInTheDocument();
    expect(await screen.findByText("ABC123")).toBeInTheDocument();
  });
});
