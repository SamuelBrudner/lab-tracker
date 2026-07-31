import * as React from "react";

import {
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";

import { App } from "./app-shell.jsx";

import { buildApiPath } from "./shared/api.js";

import { TOKEN_STORAGE_KEY } from "./shared/constants.js";

import { errorResponse, installFetchMock, textResponse } from "./test/utils.js";

import {
  activeSessionsPath,
  apiResponse,
  committedAnalysesPath,
  dataset,
  datasetCountPath,
  datasetListPath,
  graphEdge,
  graphNode,
  note,
  noteCountPath,
  paged,
  project,
  projectGraph,
  projectGraphMermaidPath,
  projectGraphPath,
  projectMembersPath,
  projectsPath,
  question,
  questionCountPath,
  questionListPath,
  questionRefactorsPath,
  recentNotesPath,
  requestedUrls,
  session,
  stagedAnalysesPath,
  targetedQuestionNotesPath,
} from "./test/fixtures.js";

describe("App", () => {
  it("accepts an emailed invitation link", async () => {
    window.history.replaceState(
      {},
      "",
      "/app/#invite=signed-token&email=member%40example.org"
    );
    installFetchMock([
      {
        match: "/auth/me",
        response: [
          errorResponse("Authentication required.", 401),
          apiResponse({
            role: "editor",
            username: "member@example.org",
            user_id: "user-1",
          }),
        ],
      },
      {
        match: "/auth/register",
        method: "POST",
        response: (request) => {
          expect(JSON.parse(request.init.body)).toEqual({
            invite_token: "signed-token",
            password: "long-invite-secret",
            password_confirmation: "long-invite-secret",
            username: "member@example.org",
          });
          return apiResponse({
            access_token: "invite-access-token",
            expires_at: "2026-06-16T12:00:00Z",
            token_type: "bearer",
            user: {
              role: "editor",
              username: "member@example.org",
              user_id: "user-1",
            },
          });
        },
      },
      {
        match: projectsPath,
        response: apiResponse([]),
      },
      {
        match: "/auth/setup-readiness",
        response: apiResponse({
          background_worker_enabled: true,
          provider: "openai",
          provider_credential_configured: false,
          scheduler_enabled: true,
          source_revision: "0123456789abcdef0123456789abcdef01234567",
        }),
      },
    ]);

    render(<App />);

    expect(await screen.findByRole("heading", { name: "Accept Invitation" })).toBeInTheDocument();
    expect(screen.getByLabelText("Username")).toHaveValue("member@example.org");
    expect(window.location.hash).toBe("");
    expect(window.location.search).toBe("");
    expect(window.location.pathname).toBe("/app/");

    fireEvent.change(screen.getByLabelText("Password"), {
      target: { value: "long-invite-secret" },
    });
    fireEvent.change(screen.getByLabelText("Confirm password"), {
      target: { value: "long-invite-secret" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Create account" }));

    await waitFor(() =>
      expect(localStorage.getItem(TOKEN_STORAGE_KEY)).toBe("invite-access-token")
    );
    expect(
      await screen.findByRole("heading", { name: "Set up your Lab Tracker" })
    ).toBeInTheDocument();
    expect(window.location.pathname).toBe("/app/setup");
    expect(screen.getByText(/automatic drafting is not ready/i)).toBeInTheDocument();
  });

  it("accepts and scrubs invitation links issued in the legacy query format", async () => {
    window.history.replaceState(
      {},
      "",
      "/app?invite=legacy-signed-token&email=member%40example.org&source=email"
    );
    installFetchMock([
      {
        match: "/auth/me",
        response: errorResponse("Authentication required.", 401),
      },
    ]);

    render(<App />);

    expect(await screen.findByRole("heading", { name: "Accept Invitation" })).toBeInTheDocument();
    expect(screen.getByLabelText("Username")).toHaveValue("member@example.org");
    expect(window.location.search).toBe("?source=email");
    expect(window.location.href).not.toContain("legacy-signed-token");
  });

  it("does not submit an invited account when password confirmation differs", async () => {
    window.history.replaceState(
      {},
      "",
      "/app/#invite=signed-token&email=member%40example.org"
    );
    const fetchMock = installFetchMock([
      {
        match: "/auth/me",
        response: errorResponse("Authentication required.", 401),
      },
    ]);

    render(<App />);

    expect(await screen.findByRole("heading", { name: "Accept Invitation" })).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Password"), {
      target: { value: "long-invite-secret" },
    });
    fireEvent.change(screen.getByLabelText("Confirm password"), {
      target: { value: "different-secret" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Create account" }));

    expect(await screen.findByText("Password confirmation does not match.")).toBeInTheDocument();
    expect(requestedUrls(fetchMock)).not.toContain("/auth/register");
  });

  it("loads projects without a token when local auth is disabled", async () => {
    const fetchMock = installFetchMock([
      {
        match: "/auth/me",
        response: apiResponse(
          { role: "admin", username: "local-tester" },
          200,
          { auth_enabled: false }
        ),
      },
      {
        match: projectsPath,
        response: apiResponse([project("project-1", "Temporal odor project")]),
      },
      {
        match: questionListPath("project-1"),
        response: paged([question({ projectId: "project-1", text: "Odor timing?" })]),
      },
      {
        match: datasetListPath("project-1"),
        response: paged([]),
      },
      {
        match: noteCountPath("project-1"),
        response: paged([], { limit: 1, offset: 0, total: 0 }),
      },
      {
        match: recentNotesPath("project-1"),
        response: paged([]),
      },
      {
        match: activeSessionsPath("project-1"),
        response: paged([]),
      },
      {
        match: stagedAnalysesPath("project-1"),
        response: paged([]),
      },
      {
        match: committedAnalysesPath("project-1"),
        response: paged([], { limit: 1, offset: 0, total: 0 }),
      },
    ]);

    render(<App />);

    const selector = await screen.findByLabelText("Active project");
    await waitFor(() => expect(selector).toHaveValue("project-1"));
    expect(screen.getAllByText("Temporal odor project").length).toBeGreaterThan(0);
    await waitFor(() => expect(screen.getAllByText("Odor timing?").length).toBeGreaterThan(0));
    expect(requestedUrls(fetchMock)).toContain(projectsPath);
  });

  it("shows portfolio home for multi-project local auth sessions", async () => {
    const portfolioPath = buildApiPath("/portfolio/summary", { limit: 100, offset: 0 });
    const fetchMock = installFetchMock([
      {
        match: "/auth/me",
        response: apiResponse(
          { role: "admin", username: "local-tester" },
          200,
          { auth_enabled: false }
        ),
      },
      {
        match: projectsPath,
        response: apiResponse([
          project("project-1", "Temporal odor"),
          project("project-2", "Fly behavior"),
        ]),
      },
      {
        match: questionListPath("project-1"),
        response: paged([question({ projectId: "project-1", text: "Odor timing?" })]),
      },
      {
        match: datasetListPath("project-1"),
        response: paged([]),
      },
      {
        match: noteCountPath("project-1"),
        response: paged([], { limit: 1, offset: 0, total: 0 }),
      },
      {
        match: recentNotesPath("project-1"),
        response: paged([]),
      },
      {
        match: activeSessionsPath("project-1"),
        response: paged([]),
      },
      {
        match: stagedAnalysesPath("project-1"),
        response: paged([]),
      },
      {
        match: committedAnalysesPath("project-1"),
        response: paged([], { limit: 1, offset: 0, total: 0 }),
      },
      {
        match: projectMembersPath("project-1"),
        response: paged([]),
      },
      {
        match: buildApiPath("/batches", { limit: 5 }),
        response: paged([], { limit: 5, offset: 0, total: 0 }),
      },
      {
        match: portfolioPath,
        response: paged([
          {
            project_count: 2,
            project_group: { group_id: "group-1", name: "Olfaction lab" },
            projects: [
              {
                committed_dataset_count: 1,
                draft_dataset_count: 0,
                last_activity_at: "2026-06-12T12:00:00Z",
                name: "Temporal odor",
                open_question_count: 2,
                owners: [{ user_id: "owner-1", username: "maya" }],
                project_id: "project-1",
                staged_analysis_count: 1,
                status: "active",
                triage_flags: [
                  {
                    count: 2,
                    key: "unanswered_questions",
                    label: "Unanswered questions",
                    severity: "warning",
                  },
                  {
                    count: 1,
                    key: "overdue_goals",
                    label: "Overdue goals",
                    severity: "critical",
                  },
                ],
                unreviewed_claim_count: 1,
              },
              {
                committed_dataset_count: 0,
                draft_dataset_count: 1,
                last_activity_at: "2026-06-11T12:00:00Z",
                name: "Fly behavior",
                open_question_count: 0,
                owners: [],
                project_id: "project-2",
                staged_analysis_count: 0,
                status: "active",
                triage_flags: [],
                unreviewed_claim_count: 0,
              },
            ],
          },
        ]),
      },
    ]);

    render(<App />);

    expect(await screen.findByRole("heading", { name: "Portfolio Home" })).toBeInTheDocument();
    expect(await screen.findByText("Olfaction lab")).toBeInTheDocument();
    expect(screen.getByText("Local single-user mode: per-trainee differentiation is unavailable until multi-user auth is enabled.")).toBeInTheDocument();
    expect(screen.getByText("Unanswered questions: 2")).toBeInTheDocument();
    expect(screen.getByText("Overdue goals: 1")).toBeInTheDocument();
    expect(screen.getByText("No triage flags")).toBeInTheDocument();
    expect(screen.getByText("Owners: maya")).toBeInTheDocument();
    expect(requestedUrls(fetchMock)).toContain(portfolioPath);

    fireEvent.click(screen.getAllByRole("button", { name: "Open project" })[1]);
    await waitFor(() => expect(screen.getByLabelText("Active project")).toHaveValue("project-2"));
  });

  it("lets a global viewer with a contributor membership use project write surfaces", async () => {
    localStorage.setItem(TOKEN_STORAGE_KEY, "token-viewer-contributor");
    installFetchMock([
      {
        match: "/auth/me",
        response: apiResponse({ role: "viewer", user_id: "user-1", username: "josh" }),
      },
      {
        match: projectsPath,
        response: apiResponse([project("project-1", "Temporal odor")]),
      },
      {
        match: questionListPath("project-1"),
        response: paged([]),
      },
      {
        match: questionCountPath("project-1"),
        response: paged([], { limit: 1, offset: 0, total: 0 }),
      },
      {
        match: datasetListPath("project-1"),
        response: paged([]),
      },
      {
        match: datasetCountPath("project-1"),
        response: paged([], { limit: 1, offset: 0, total: 0 }),
      },
      {
        match: noteCountPath("project-1"),
        response: paged([], { limit: 1, offset: 0, total: 0 }),
      },
      {
        match: recentNotesPath("project-1"),
        response: paged([]),
      },
      {
        match: activeSessionsPath("project-1"),
        response: paged([]),
      },
      {
        match: stagedAnalysesPath("project-1"),
        response: paged([]),
      },
      {
        match: committedAnalysesPath("project-1"),
        response: paged([], { limit: 1, offset: 0, total: 0 }),
      },
      {
        match: projectMembersPath("project-1"),
        response: paged([
          {
            membership_id: "membership-1",
            role: "contributor",
            user_id: "user-1",
            username: "josh",
          },
        ]),
      },
      {
        match: buildApiPath("/batches", { limit: 5 }),
        response: paged([], { limit: 5, offset: 0, total: 0 }),
      },
    ]);

    render(<App />);

    // Project-scoped write surfaces follow the project membership, not the
    // global role: the contributor can stage questions in this project.
    await waitFor(() => {
      expect(screen.getByLabelText("Question text")).toBeEnabled();
    });
    expect(screen.getByRole("button", { name: "Stage question" })).toBeEnabled();

    // Genuinely global actions stay gated on the global role.
    expect(screen.getByRole("button", { name: "Create project" })).toBeDisabled();
    expect(document.querySelector('[name="new-project-name"]')).toBeDisabled();
  });

  it("restores a stored session and signs out", async () => {
    localStorage.setItem(TOKEN_STORAGE_KEY, "token-1");
    installFetchMock([
      {
        match: "/auth/me",
        response: apiResponse({ role: "admin", username: "sam" }),
      },
      {
        match: projectsPath,
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
        match: projectsPath,
        response: apiResponse([]),
      },
      {
        match: `/questions/${questionId}`,
        response: apiResponse(
          question({
            questionId,
            text: "How stable is the rig today?",
          })
        ),
      },
      {
        match: questionListPath("project-1"),
        response: paged([
          question({
            questionId,
            text: "How stable is the rig today?",
          }),
        ]),
      },
      {
        match: targetedQuestionNotesPath("project-1", questionId),
        response: paged([]),
      },
      {
        match: questionRefactorsPath(questionId),
        response: paged([]),
      },
    ]);

    render(<App />);

    expect(await screen.findByRole("heading", { name: "Question Detail" })).toBeInTheDocument();
    expect(await screen.findByText("How stable is the rig today?")).toBeInTheDocument();
  });

  it("renders the project graph route, switches views, exports Mermaid, and navigates nodes", async () => {
    window.history.replaceState({}, "", "/app/graph");
    const datasetId = "11111111-1111-4111-8111-111111111111";
    const questionId = "22222222-2222-4222-8222-222222222222";
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText },
    });
    const evidenceGraph = projectGraph({
      nodes: [
        graphNode({
          entityId: questionId,
          entityType: "question",
          label: "Can we see evidence?",
          route: `/app/questions/${questionId}`,
          status: "active",
        }),
        graphNode({
          entityId: datasetId,
          entityType: "dataset",
          label: "Dataset commit-1",
          route: `/app/datasets/${datasetId}`,
          status: "committed",
        }),
      ],
      edges: [
        graphEdge({
          label: "primary question",
          relationship: "dataset_question_primary",
          source: `question:${questionId}`,
          target: `dataset:${datasetId}`,
        }),
      ],
    });
    const questionsGraph = projectGraph({
      view: "questions",
      nodes: [
        graphNode({
          entityId: questionId,
          entityType: "question",
          label: "Can we see evidence?",
          route: `/app/questions/${questionId}`,
          status: "active",
        }),
      ],
    });

    const fetchMock = installFetchMock([
      {
        match: "/auth/me",
        response: apiResponse(
          { role: "admin", username: "local-tester" },
          200,
          { auth_enabled: false }
        ),
      },
      {
        match: projectsPath,
        response: apiResponse([project("project-1", "Temporal odor project")]),
      },
      {
        match: questionCountPath("project-1"),
        response: paged([question({ questionId })], { limit: 1, offset: 0, total: 1 }),
      },
      {
        match: datasetCountPath("project-1"),
        response: paged([dataset({ datasetId })], { limit: 1, offset: 0, total: 1 }),
      },
      {
        match: noteCountPath("project-1"),
        response: paged([], { limit: 1, offset: 0, total: 0 }),
      },
      {
        match: projectMembersPath("project-1"),
        response: paged([]),
      },
      {
        match: projectGraphPath("project-1", "evidence"),
        response: apiResponse(evidenceGraph),
      },
      {
        match: projectGraphPath("project-1", "questions"),
        response: apiResponse(questionsGraph),
      },
      {
        match: projectGraphMermaidPath("project-1", "questions"),
        response: textResponse("graph LR\n  n0[\"question\"]\n", 200, "text/vnd.mermaid"),
      },
      {
        match: `/datasets/${datasetId}`,
        response: apiResponse(
          dataset({
            datasetId,
            status: "committed",
          })
        ),
      },
    ]);

    render(<App />);

    expect(await screen.findByRole("heading", { name: "Project Graph" })).toBeInTheDocument();
    expect(await screen.findByTestId("react-flow")).toBeInTheDocument();
    expect(screen.getByText("Evidence flow")).toBeInTheDocument();
    expect(screen.getByText("Question: 1")).toBeInTheDocument();
    expect(screen.getByText("Dataset: 1")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Dataset commit-1" })).toBeInTheDocument();
    expect(requestedUrls(fetchMock)).toContain(projectGraphPath("project-1", "evidence"));

    fireEvent.click(screen.getByRole("tab", { name: "Questions" }));
    await waitFor(() =>
      expect(requestedUrls(fetchMock)).toContain(projectGraphPath("project-1", "questions"))
    );
    expect(await screen.findByText("Question links")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Copy Mermaid" }));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith("graph LR\n  n0[\"question\"]\n"));
    expect(await screen.findByText("Mermaid graph copied.")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("tab", { name: "Evidence" }));
    await screen.findByRole("button", { name: "Dataset commit-1" });
    // Clicking a node opens the in-place detail panel; navigation happens
    // through the panel's explicit Open button.
    fireEvent.click(screen.getByRole("button", { name: "Dataset commit-1" }));
    const detailPanel = await screen.findByRole("complementary", {
      name: "Selected node details",
    });
    expect(detailPanel).toHaveTextContent("Status: committed");
    fireEvent.click(screen.getByRole("button", { name: "Open Dataset" }));
    await waitFor(() => expect(window.location.pathname).toBe(`/app/datasets/${datasetId}`));
    expect(await screen.findByRole("heading", { name: "Dataset Detail" })).toBeInTheDocument();
  });

  it("shows a visible restore error when session bootstrap fails", async () => {
    localStorage.setItem(TOKEN_STORAGE_KEY, "token-3");
    installFetchMock([
      {
        match: "/auth/me",
        response: errorResponse("Session expired.", 401),
      },
      {
        match: projectsPath,
        response: apiResponse([]),
      },
    ]);

    render(<App />);

    expect(
      await screen.findByText("Your session expired. Please sign in again.")
    ).toBeInTheDocument();
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
        match: projectsPath,
        response: apiResponse([project("project-1", "Project One")]),
      },
      {
        match: questionCountPath("project-1"),
        response: paged([question()], { limit: 1, offset: 0, total: 12 }),
      },
      {
        match: datasetCountPath("project-1"),
        response: paged([dataset()], { limit: 1, offset: 0, total: 4 }),
      },
      {
        match: noteCountPath("project-1"),
        response: paged([note()], { limit: 1, offset: 0, total: 7 }),
      },
      {
        match: `/questions/${questionId}`,
        response: apiResponse(
          question({
            questionId,
            text: "How stable is the rig today?",
          })
        ),
      },
      {
        match: questionListPath("project-1"),
        response: paged([
          question({
            questionId,
            text: "How stable is the rig today?",
          }),
        ]),
      },
      {
        match: targetedQuestionNotesPath("project-1", questionId),
        response: paged([]),
      },
      {
        match: questionRefactorsPath(questionId),
        response: paged([]),
      },
    ]);

    render(<App />);

    expect(await screen.findByRole("heading", { name: "Question Detail" })).toBeInTheDocument();
    expect(await screen.findByText("How stable is the rig today?")).toBeInTheDocument();
    expect(await screen.findByText("12")).toBeInTheDocument();
    expect(await screen.findByText("4")).toBeInTheDocument();
    expect(await screen.findByText("7")).toBeInTheDocument();
  });

  it("loads reduced-scope home data and refreshes when the active project changes", async () => {
    localStorage.setItem(TOKEN_STORAGE_KEY, "token-home-reduced");

    const firstProjectQuestions = Array.from({ length: 205 }, (_, index) =>
      question({
        projectId: "project-1",
        questionId: `question-1-${index}`,
        status: "staged",
        text: `Project One Question ${index}`,
      })
    );
    const secondProjectQuestions = Array.from({ length: 3 }, (_, index) =>
      question({
        projectId: "project-2",
        questionId: `question-2-${index}`,
        status: "staged",
        text: `Project Two Question ${index}`,
      })
    );

    const fetchMock = installFetchMock([
      {
        match: "/auth/me",
        response: apiResponse({ role: "admin", username: "sam" }),
      },
      {
        match: projectsPath,
        response: apiResponse([project("project-1", "Project One"), project("project-2", "Project Two")]),
      },
      {
        match: questionListPath("project-1"),
        response: paged(firstProjectQuestions.slice(0, 200), {
          limit: 200,
          offset: 0,
          total: 205,
        }),
      },
      {
        match: questionListPath("project-1", { offset: 200 }),
        response: paged(firstProjectQuestions.slice(200), {
          limit: 200,
          offset: 200,
          total: 205,
        }),
      },
      {
        match: datasetListPath("project-1"),
        response: apiResponse([]),
      },
      {
        match: noteCountPath("project-1"),
        response: paged([note({ noteId: "note-1" })], {
          limit: 1,
          offset: 0,
          total: 4,
        }),
      },
      {
        match: recentNotesPath("project-1"),
        response: paged([note({ noteId: "note-1", transcribedText: "Project One note" })], {
          limit: 5,
          offset: 0,
          total: 1,
        }),
      },
      {
        match: activeSessionsPath("project-1"),
        response: paged([session({ sessionId: "session-1", linkCode: "P1CODE" })]),
      },
      {
        match: stagedAnalysesPath("project-1"),
        response: paged([]),
      },
      {
        match: committedAnalysesPath("project-1"),
        response: paged([], { limit: 1, offset: 0, total: 0 }),
      },
      {
        match: questionListPath("project-2"),
        response: paged(secondProjectQuestions),
      },
      {
        match: datasetListPath("project-2"),
        response: apiResponse([]),
      },
      {
        match: noteCountPath("project-2"),
        response: paged([note({ noteId: "note-2", projectId: "project-2" })], {
          limit: 1,
          offset: 0,
          total: 2,
        }),
      },
      {
        match: recentNotesPath("project-2"),
        response: paged([note({ noteId: "note-2", projectId: "project-2", transcribedText: "Project Two note" })], {
          limit: 5,
          offset: 0,
          total: 1,
        }),
      },
      {
        match: activeSessionsPath("project-2"),
        response: paged([session({ sessionId: "session-2", linkCode: "P2CODE", projectId: "project-2" })]),
      },
      {
        match: stagedAnalysesPath("project-2"),
        response: paged([]),
      },
      {
        match: committedAnalysesPath("project-2"),
        response: paged([], { limit: 1, offset: 0, total: 0 }),
      },
    ]);

    render(<App />);

    expect((await screen.findAllByText("Project One Question 204")).length).toBeGreaterThan(0);
    expect(await screen.findByText("Project One note")).toBeInTheDocument();
    expect(await screen.findByText("P1CODE")).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("Active project"), {
      target: { value: "project-2" },
    });

    expect((await screen.findAllByText("Project Two Question 2")).length).toBeGreaterThan(0);
    expect(await screen.findByText("Project Two note")).toBeInTheDocument();
    expect(await screen.findByText("P2CODE")).toBeInTheDocument();

    const urls = requestedUrls(fetchMock);
    expect(urls).toContain(recentNotesPath("project-1"));
    expect(urls).toContain(activeSessionsPath("project-1"));
    expect(urls).toContain(committedAnalysesPath("project-1"));
    expect(urls).not.toContain(buildApiPath("/notes", { project_id: "project-1", limit: 200, offset: 0 }));
    expect(urls).not.toContain(buildApiPath("/sessions", { project_id: "project-1", limit: 200, offset: 0 }));
    expect(urls.some((url) => url.startsWith("/visualizations?project_id="))).toBe(false);
  });

});
