import * as React from "react";

import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";

import { App } from "./app-shell.jsx";
import { buildApiPath } from "./shared/api.js";
import { TOKEN_STORAGE_KEY } from "./shared/constants.js";
import { apiResponse, errorResponse, installFetchMock, textResponse } from "./test/utils.js";

const projectsPath = buildApiPath("/projects", { limit: 200, offset: 0 });

function questionListPath(projectId, { limit = 200, offset = 0, ...rest } = {}) {
  return buildApiPath("/questions", {
    project_id: projectId,
    ...rest,
    limit,
    offset,
  });
}

function questionCountPath(projectId) {
  return buildApiPath("/questions", { project_id: projectId, limit: 1, offset: 0 });
}

function datasetListPath(projectId, { limit = 200, offset = 0, ...rest } = {}) {
  return buildApiPath("/datasets", {
    project_id: projectId,
    ...rest,
    limit,
    offset,
  });
}

function datasetCountPath(projectId) {
  return buildApiPath("/datasets", { project_id: projectId, limit: 1, offset: 0 });
}

function noteCountPath(projectId) {
  return buildApiPath("/notes", { project_id: projectId, limit: 1, offset: 0 });
}

function recentNotesPath(projectId) {
  return buildApiPath("/notes", { limit: 5, offset: 0, project_id: projectId });
}

function targetedQuestionNotesPath(projectId, questionId) {
  return buildApiPath("/notes", {
    project_id: projectId,
    target_entity_type: "question",
    target_entity_id: questionId,
    limit: 200,
    offset: 0,
  });
}

function questionRefactorsPath(questionId) {
  return buildApiPath(`/questions/${questionId}/refactors`, { limit: 50, offset: 0 });
}

function activeSessionsPath(projectId) {
  return buildApiPath("/sessions", {
    project_id: projectId,
    status: "active",
    limit: 200,
    offset: 0,
  });
}

function stagedAnalysesPath(projectId) {
  return buildApiPath("/analyses", {
    project_id: projectId,
    status: "staged",
    limit: 200,
    offset: 0,
  });
}

function committedAnalysesMetaPath(projectId) {
  return buildApiPath("/analyses", {
    limit: 1,
    offset: 0,
    project_id: projectId,
    status: "committed",
  });
}

function committedAnalysesRecentPath(projectId, total) {
  return buildApiPath("/analyses", {
    limit: 5,
    offset: Math.max(total - 5, 0),
    project_id: projectId,
    status: "committed",
  });
}

function captureAnalysesPath(projectId) {
  return buildApiPath("/analyses", { project_id: projectId, limit: 50 });
}

function captureClaimsPath(projectId) {
  return buildApiPath("/claims", { project_id: projectId, limit: 50 });
}

function datasetFilesPath(datasetId) {
  return buildApiPath(`/datasets/${datasetId}/files`, { limit: 200, offset: 0 });
}

function visualizationsPath(analysisId) {
  return buildApiPath("/visualizations", {
    analysis_id: analysisId,
    limit: 200,
    offset: 0,
  });
}

function projectMembersPath(projectId) {
  return buildApiPath(`/projects/${projectId}/members`, { limit: 200 });
}

function projectGraphPath(projectId, view = "evidence") {
  return buildApiPath(`/projects/${projectId}/graph`, { view });
}

function projectGraphMermaidPath(projectId, view = "evidence") {
  return buildApiPath(`/projects/${projectId}/graph/mermaid`, { view });
}

function paged(data, { limit = 200, offset = 0, total = data.length } = {}) {
  return apiResponse(data, 200, { limit, offset, total });
}

function project(projectId, name) {
  return { name, project_id: projectId };
}

function question({
  createdAt = "2026-04-20T00:00:00Z",
  hypothesis = null,
  parentQuestionIds = [],
  projectId = "project-1",
  questionType = "descriptive",
  questionId = "question-1",
  status = "active",
  supersededByQuestionId = null,
  supersedesQuestionId = null,
  text = "Question",
  updatedAt = "2026-04-20T01:00:00Z",
} = {}) {
  return {
    created_at: createdAt,
    hypothesis,
    parent_question_ids: parentQuestionIds,
    project_id: projectId,
    question_id: questionId,
    question_type: questionType,
    status,
    superseded_by_question_id: supersededByQuestionId,
    supersedes_question_id: supersedesQuestionId,
    text,
    updated_at: updatedAt,
  };
}

function dataset({
  commitHash = "commit-1",
  createdAt = "2026-04-20T00:00:00Z",
  datasetId = "dataset-1",
  primaryQuestionId = "question-1",
  projectId = "project-1",
  questionLinks = null,
  status = "staged",
  updatedAt = "2026-04-20T01:00:00Z",
} = {}) {
  return {
    commit_hash: commitHash,
    created_at: createdAt,
    dataset_id: datasetId,
    primary_question_id: primaryQuestionId,
    project_id: projectId,
    question_links:
      questionLinks || [{ outcome_status: "unknown", question_id: primaryQuestionId, role: "primary" }],
    status,
    updated_at: updatedAt,
  };
}

function note({
  createdAt = "2026-04-20T00:00:00Z",
  metadata = {},
  noteId = "note-1",
  projectId = "project-1",
  rawAsset = null,
  rawContent = "",
  status = "staged",
  targets = [],
  transcribedText = "Captured note",
} = {}) {
  return {
    created_at: createdAt,
    metadata,
    note_id: noteId,
    project_id: projectId,
    raw_asset: rawAsset,
    raw_content: rawContent,
    status,
    targets,
    transcribed_text: transcribedText,
    updated_at: createdAt,
  };
}

function session({
  linkCode = "ABC123",
  primaryQuestionId = "question-1",
  projectId = "project-1",
  sessionId = "session-1",
  sessionType = "scientific",
  startedAt = "2026-04-20T03:00:00Z",
  status = "active",
} = {}) {
  return {
    link_code: linkCode,
    primary_question_id: primaryQuestionId,
    project_id: projectId,
    session_id: sessionId,
    session_type: sessionType,
    started_at: startedAt,
    status,
  };
}

function analysis({
  analysisId = "analysis-1",
  codeVersion = "sha-1",
  createdAt = "2026-04-20T00:00:00Z",
  datasetIds = ["dataset-1"],
  environmentHash = null,
  executedAt = "2026-04-20T02:00:00Z",
  methodHash = "method-1",
  projectId = "project-1",
  status = "staged",
  updatedAt = "2026-04-20T02:00:00Z",
} = {}) {
  return {
    analysis_id: analysisId,
    code_version: codeVersion,
    created_at: createdAt,
    dataset_ids: datasetIds,
    environment_hash: environmentHash,
    executed_at: executedAt,
    method_hash: methodHash,
    project_id: projectId,
    status,
    updated_at: updatedAt,
  };
}

function claim({
  claimId = "claim-1",
  confidence = 62,
  createdAt = "2026-04-20T02:00:00Z",
  projectId = "project-1",
  statement = "Turning appears stronger after pulse onset.",
  status = "proposed",
} = {}) {
  return {
    claim_id: claimId,
    confidence,
    created_at: createdAt,
    project_id: projectId,
    statement,
    status,
    supported_by_analysis_ids: [],
    supported_by_dataset_ids: [],
    updated_at: createdAt,
  };
}

function visualization({
  analysisId = "analysis-1",
  createdAt = "2026-04-20T02:00:00Z",
  filePath = "viz/output.png",
  vizId = "viz-1",
  vizType = "timeseries",
} = {}) {
  return {
    analysis_id: analysisId,
    created_at: createdAt,
    file_path: filePath,
    viz_id: vizId,
    viz_type: vizType,
  };
}

function projectGraph({
  edges = [],
  nodes = [],
  projectId = "project-1",
  view = "evidence",
} = {}) {
  return {
    edges,
    nodes,
    project_id: projectId,
    view,
  };
}

function graphNode({
  detail = null,
  entityId,
  entityType,
  label,
  route = null,
  status = null,
} = {}) {
  return {
    detail,
    entity_id: entityId,
    entity_type: entityType,
    id: `${entityType}:${entityId}`,
    label,
    metadata: {},
    route,
    status,
  };
}

function graphEdge({ label, relationship, source, target } = {}) {
  return {
    id: `${relationship}:${source}->${target}`,
    label,
    relationship,
    source,
    target,
  };
}

function requestedUrls(fetchMock) {
  return fetchMock.mock.calls.map(([input]) => (typeof input === "string" ? input : input.url));
}

describe("App", () => {
  it("accepts an emailed invitation link", async () => {
    window.history.replaceState(
      {},
      "",
      "/app?invite=signed-token&email=member%40example.org"
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
            password: "invite-secret",
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
    ]);

    render(<App />);

    expect(await screen.findByRole("heading", { name: "Accept Invitation" })).toBeInTheDocument();
    expect(screen.getByLabelText("Username")).toHaveValue("member@example.org");

    fireEvent.change(screen.getByLabelText("Password"), {
      target: { value: "invite-secret" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Create account" }));

    await waitFor(() =>
      expect(localStorage.getItem(TOKEN_STORAGE_KEY)).toBe("invite-access-token")
    );
    expect(await screen.findByText("Welcome to Lab Tracker")).toBeInTheDocument();
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
        match: committedAnalysesMetaPath("project-1"),
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
        match: committedAnalysesMetaPath("project-1"),
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
    expect(screen.getByText("Olfaction lab")).toBeInTheDocument();
    expect(screen.getByText("Local single-user mode: per-trainee differentiation is unavailable until multi-user auth is enabled.")).toBeInTheDocument();
    expect(screen.getByText("Unanswered questions: 2")).toBeInTheDocument();
    expect(screen.getByText("Overdue goals: 1")).toBeInTheDocument();
    expect(screen.getByText("No triage flags")).toBeInTheDocument();
    expect(screen.getByText("Owners: maya")).toBeInTheDocument();
    expect(requestedUrls(fetchMock)).toContain(portfolioPath);

    fireEvent.click(screen.getAllByRole("button", { name: "Open project" })[1]);
    await waitFor(() => expect(screen.getByLabelText("Active project")).toHaveValue("project-2"));
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
        response: paged([], { limit: 1, offset: 0, total: 1 }),
      },
      {
        match: datasetCountPath("project-1"),
        response: paged([], { limit: 1, offset: 0, total: 1 }),
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
    fireEvent.click(screen.getByRole("button", { name: "Dataset commit-1" }));
    await waitFor(() => expect(window.location.pathname).toBe(`/app/datasets/${datasetId}`));
    expect(await screen.findByRole("heading", { name: "Dataset Detail" })).toBeInTheDocument();
  });

  it("previews an image note and starts a graph draft", async () => {
    const noteId = "11111111-1111-4111-8111-111111111111";
    const draftId = "22222222-2222-4222-8222-222222222222";
    localStorage.setItem(TOKEN_STORAGE_KEY, "token-note-draft");
    window.history.replaceState({}, "", `/app/notes/${noteId}`);

    installFetchMock([
      {
        match: "/auth/me",
        response: apiResponse({ role: "admin", username: "sam" }),
      },
      {
        match: projectsPath,
        response: apiResponse([]),
      },
      {
        match: `/notes/${noteId}`,
        response: apiResponse(
          note({
            noteId,
            rawAsset: {
              checksum: "abc",
              content_type: "image/jpeg",
              filename: "whiteboard.jpg",
              size_bytes: 4,
              storage_id: "33333333-3333-4333-8333-333333333333",
            },
          })
        ),
      },
      {
        match: `/notes/${noteId}/raw`,
        response: apiResponse({
          checksum: "abc",
          content_base64: "aW1n",
          content_type: "image/jpeg",
          filename: "whiteboard.jpg",
          size_bytes: 4,
          storage_id: "33333333-3333-4333-8333-333333333333",
        }),
      },
      {
        match: `/notes/${noteId}/graph-drafts`,
        method: "POST",
        response: apiResponse({
          change_set_id: draftId,
          clarification_requests: [],
          context_packet: { mode: "graph_context" },
          created_at: "2026-04-20T00:00:00Z",
          draft_mode: "graph_context",
          model: "gpt-5.4-mini",
          operations: [],
          project_id: "project-1",
          prompt_version: "image-graph-draft-v2",
          provider: "openai",
          source_content_type: "image/jpeg",
          source_note_id: noteId,
          status: "ready",
          summary: "Draft ready",
          uncertain_fields: [],
          updated_at: "2026-04-20T00:00:00Z",
        }),
      },
      {
        match: `/graph-drafts/${draftId}`,
        response: apiResponse({
          change_set_id: draftId,
          clarification_requests: [],
          context_packet: { mode: "graph_context" },
          created_at: "2026-04-20T00:00:00Z",
          draft_mode: "graph_context",
          model: "gpt-5.4-mini",
          operations: [],
          project_id: "project-1",
          prompt_version: "image-graph-draft-v2",
          provider: "openai",
          source_content_type: "image/jpeg",
          source_note_id: noteId,
          status: "ready",
          summary: "Draft ready",
          uncertain_fields: [],
          updated_at: "2026-04-20T00:00:00Z",
        }),
      },
    ]);

    render(<App />);

    expect(await screen.findByRole("heading", { name: "Note Detail" })).toBeInTheDocument();
    expect(await screen.findByAltText("whiteboard.jpg")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Draft graph update" }));

    expect(await screen.findByRole("heading", { name: "Graph Draft Review" })).toBeInTheDocument();
    expect(await screen.findByText("Graph draft ready for review.")).toBeInTheDocument();
  });

  it("reviews, edits, accepts, and commits a graph draft", async () => {
    const noteId = "11111111-1111-4111-8111-111111111111";
    const draftId = "22222222-2222-4222-8222-222222222222";
    const operationId = "33333333-3333-4333-8333-333333333333";
    const questionId = "44444444-4444-4444-8444-444444444444";
    const baseOperation = {
      change_set_id: draftId,
      client_ref: "q1",
      confidence: 0.82,
      created_at: "2026-04-20T00:00:00Z",
      entity_type: "question",
      error_metadata: {},
      op: "create",
      operation_id: operationId,
      payload: {
        project_id: "project-1",
        question_type: "descriptive",
        text: "Drafted question",
      },
      rationale: "The whiteboard asks this explicitly.",
      result_entity_id: null,
      sequence: 1,
      semantic_type: "suggest_new_question",
      source_refs: [
        {
          label: "whiteboard",
          quote: "yield?",
          region: { height: 0.2, width: 0.3, x: 0.1, y: 0.2 },
        },
      ],
      status: "proposed",
      target_entity_id: null,
      updated_at: "2026-04-20T00:00:00Z",
    };
    const draftBase = {
      change_set_id: draftId,
      clarification_requests: ["Confirm whether Fly 12 should be formalized."],
      context_packet: {
        active_or_staged_questions: [
          { id: questionId, label: "Existing question", status: "active" },
        ],
        context_summary: {
          approximate_size_bytes: 1234,
          counts: {
            active_or_staged_questions: 1,
            known_aliases: 1,
            projects: 1,
            recent_analyses: 0,
            recent_claims: 0,
            recent_datasets: 0,
            recent_notes: 0,
            recent_sessions: 0,
            recent_visualizations: 0,
            selected_targets: 0,
            source_artifacts: 1,
            unresolved_recent_captures: 0,
          },
          selected_targets: [],
          source_artifact_counts: { image: 1 },
          warnings: ["no audio source artifact was included"],
        },
        mode: "graph_context",
        project: { id: "project-1", label: "Project One" },
      },
      created_at: "2026-04-20T00:00:00Z",
      draft_mode: "graph_context",
      model: "gpt-5.4-mini",
      operations: [baseOperation],
      project_id: "project-1",
      prompt_version: "image-graph-draft-v2",
      provider: "openai",
      source_content_type: "image/jpeg",
      source_filename: "whiteboard.jpg",
      source_note_id: noteId,
      status: "ready",
      summary: "Drafted one question from the whiteboard.",
      uncertain_fields: ["Exact protocol name"],
      updated_at: "2026-04-20T00:00:00Z",
    };

    localStorage.setItem(TOKEN_STORAGE_KEY, "token-graph-draft");
    window.history.replaceState({}, "", `/app/graph-drafts/${draftId}`);

    installFetchMock([
      {
        match: "/auth/me",
        response: apiResponse({ role: "admin", username: "sam" }),
      },
      {
        match: projectsPath,
        response: apiResponse([]),
      },
      {
        match: `/graph-drafts/${draftId}`,
        response: apiResponse(draftBase),
      },
      {
        match: `/notes/${noteId}/raw`,
        response: apiResponse({
          checksum: "abc",
          content_base64: "aW1n",
          content_type: "image/jpeg",
          filename: "whiteboard.jpg",
          size_bytes: 4,
          storage_id: "55555555-5555-4555-8555-555555555555",
        }),
      },
      {
        match: `/graph-drafts/${draftId}/operations/${operationId}`,
        method: "PATCH",
        response: (request) => {
          const body = JSON.parse(request.init.body);
          expect(body.status).toBe("accepted");
          expect(body.payload.text).toBe("Edited draft question");
          return apiResponse({
            ...draftBase,
            operations: [
              {
                ...baseOperation,
                payload: body.payload,
                status: "accepted",
              },
            ],
          });
        },
      },
      {
        match: `/graph-drafts/${draftId}/commit`,
        method: "POST",
        response: (request) => {
          const body = JSON.parse(request.init.body);
          expect(body.message).toBe("Commit image draft");
          return apiResponse({
            ...draftBase,
            commit_message: "Commit image draft",
            operations: [
              {
                ...baseOperation,
                result_entity_id: questionId,
                status: "applied",
              },
            ],
            status: "committed",
          });
        },
      },
    ]);

    render(<App />);

    expect(await screen.findByRole("heading", { name: "Graph Draft Review" })).toBeInTheDocument();
    expect(await screen.findByText("Drafted one question from the whiteboard.")).toBeInTheDocument();
    expect(screen.getByText("Context summary")).toBeInTheDocument();
    expect(screen.getByText("~1234 bytes")).toBeInTheDocument();
    expect(screen.getByText("Source artifacts: image 1")).toBeInTheDocument();
    expect(screen.getByText("no audio source artifact was included")).toBeInTheDocument();
    expect(screen.getByText("Exact protocol name")).toBeInTheDocument();
    expect(screen.getByText("Confirm whether Fly 12 should be formalized.")).toBeInTheDocument();
    expect(screen.getByText("suggest new question")).toBeInTheDocument();
    expect(screen.getByText("Proposed new question")).toBeInTheDocument();
    expect(screen.getByText("Model inference")).toBeInTheDocument();
    expect(screen.getByText("Source evidence")).toBeInTheDocument();
    expect(screen.getByText("Payload JSON (advanced)")).toBeInTheDocument();
    expect(await screen.findByLabelText("Source region 1: whiteboard")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Text"), {
      target: { value: "Edited draft question" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Accept" }));

    expect(await screen.findByText("accepted")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Commit message"), {
      target: { value: "Commit image draft" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Commit accepted changes" }));

    expect(await screen.findByText("Graph draft committed.")).toBeInTheDocument();
    expect(await screen.findByText("applied")).toBeInTheDocument();
    expect(await screen.findByText(questionId)).toBeInTheDocument();
  });

  it("puts capture actions before upload details on the mobile capture route", async () => {
    localStorage.setItem(TOKEN_STORAGE_KEY, "token-mobile-capture-order");
    window.history.replaceState({}, "", "/app/capture?install=1");

    installFetchMock([
      {
        match: "/auth/me",
        response: apiResponse({ role: "admin", username: "sam" }),
      },
      {
        match: projectsPath,
        response: apiResponse([project("project-1", "Project One")]),
      },
      {
        match: questionListPath("project-1"),
        response: paged([]),
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
        match: activeSessionsPath("project-1"),
        response: paged([]),
      },
      {
        match: buildApiPath("/graph-drafts", { project_id: "project-1", limit: 10 }),
        response: paged([]),
      },
      {
        match: buildApiPath("/notes", { project_id: "project-1", limit: 10 }),
        response: paged([]),
      },
      {
        match: captureAnalysesPath("project-1"),
        response: paged([]),
      },
      {
        match: captureClaimsPath("project-1"),
        response: paged([]),
      },
      {
        match: projectGraphPath("project-1", "evidence"),
        response: apiResponse(projectGraph()),
      },
    ]);

    const { container } = render(<App />);

    const installPrompt = await screen.findByRole("status");
    const captureHeading = await screen.findByRole("heading", { name: "Capture" });
    const uploadButton = screen.getByRole("button", { name: "Upload and draft" });
    const detailsHeading = screen.getByRole("heading", { name: "Upload details" });
    const dashboardHeading = screen.getByRole("heading", { name: "Dashboard" });
    const projectSelect = screen.getByLabelText("Project");

    expect(container.querySelector(".app-shell")).toHaveClass("capture-app-shell");
    expect(
      Boolean(
        installPrompt.compareDocumentPosition(captureHeading) & Node.DOCUMENT_POSITION_FOLLOWING
      )
    ).toBe(true);
    expect(
      Boolean(
        captureHeading.compareDocumentPosition(dashboardHeading) &
          Node.DOCUMENT_POSITION_FOLLOWING
      )
    ).toBe(true);
    expect(screen.getByRole("button", { name: "Add attachment" })).toBeInTheDocument();
    expect(screen.getByLabelText("Message or hint")).toBeInTheDocument();
    expect(screen.getByLabelText("Photo file")).toBeInTheDocument();
    expect(screen.getByLabelText("Voice recording")).toBeInTheDocument();
    expect(
      Boolean(uploadButton.compareDocumentPosition(detailsHeading) & Node.DOCUMENT_POSITION_FOLLOWING)
    ).toBe(true);
    expect(
      Boolean(uploadButton.compareDocumentPosition(projectSelect) & Node.DOCUMENT_POSITION_FOLLOWING)
    ).toBe(true);

    fireEvent.click(screen.getByRole("button", { name: "Open graph" }));
    await waitFor(() => expect(window.location.pathname).toBe("/app/graph"));
    expect(await screen.findByRole("heading", { name: "Project Graph" })).toBeInTheDocument();
  });

  it("shows pending batch notifications and the batch review queue", async () => {
    const batchId = "22222222-2222-4222-8222-222222222222";
    const noteA = "11111111-1111-4111-8111-111111111111";
    const noteB = "33333333-3333-4333-8333-333333333333";
    localStorage.setItem(TOKEN_STORAGE_KEY, "token-batches");
    localStorage.setItem("lab-tracker:last-used-project-id", "project-1");
    window.history.replaceState({}, "", "/app/batches");
    const pendingBatch = {
      batch_key: "batch:test",
      batch_window_end: "2026-06-10T12:00:00Z",
      batch_window_start: "2026-06-10T00:00:00Z",
      change_set_id: batchId,
      clarification_requests: [],
      context_packet: { mode: "graph_batch" },
      created_at: "2026-06-10T12:10:00Z",
      draft_mode: "graph_batch",
      model: "fake-batch-model",
      operations: [
        {
          operation_id: "44444444-4444-4444-8444-444444444444",
          status: "proposed",
        },
      ],
      project_id: "project-1",
      provider: "fake",
      source_note_count: 2,
      source_note_id: noteA,
      source_note_ids: [noteA, noteB],
      status: "ready",
      summary: "Batch drafted one question",
      uncertain_fields: [],
      updated_at: "2026-06-10T12:10:00Z",
    };

    installFetchMock([
      {
        match: "/auth/me",
        response: apiResponse({ role: "admin", username: "sam" }),
      },
      {
        match: projectsPath,
        response: apiResponse([project("project-1", "Project One")]),
      },
      {
        match: questionListPath("project-1"),
        response: paged([]),
      },
      {
        match: datasetListPath("project-1"),
        response: paged([]),
      },
      {
        match: noteCountPath("project-1"),
        response: paged([], { limit: 1, offset: 0, total: 2 }),
      },
      {
        match: activeSessionsPath("project-1"),
        response: paged([]),
      },
      {
        match: projectMembersPath("project-1"),
        response: paged([
          {
            membership_id: "membership-1",
            project_id: "project-1",
            role: "owner",
            user_id: "user-1",
            username: "sam",
          },
        ]),
      },
      {
        match: buildApiPath("/batches", { limit: 5 }),
        response: paged([pendingBatch], { limit: 5, offset: 0, total: 1 }),
      },
      {
        match: buildApiPath("/batches", { limit: 100 }),
        response: paged([pendingBatch], { limit: 100, offset: 0, total: 1 }),
      },
      {
        match: buildApiPath("/batches", { project_id: "project-1", limit: 100 }),
        response: paged([pendingBatch], { limit: 100, offset: 0, total: 1 }),
      },
      {
        match: buildApiPath("/batches/runs", { project_id: "project-1", limit: 20 }),
        response: paged([
          {
            batch_key: "batch:test",
            note_count: 2,
            project_id: "project-1",
            run_id: "55555555-5555-4555-8555-555555555555",
            status: "ready",
            summary: "Batch drafted one question",
            trigger: "scheduled",
            window_end: "2026-06-10T12:00:00Z",
            window_start: "2026-06-10T00:00:00Z",
          },
        ]),
      },
      {
        match: `/projects/project-1/graph-draft-batch-settings`,
        response: apiResponse({
          cadence_minutes: 1440,
          enabled: true,
          next_run_at: "2026-06-11T10:00:00Z",
          project_id: "project-1",
          run_at_local_time: "06:00",
          settings_id: "66666666-6666-4666-8666-666666666666",
          timezone_name: "America/New_York",
        }),
      },
    ]);

    render(<App />);

    expect(await screen.findByText("1 graph-draft batch ready")).toBeInTheDocument();
    expect(await screen.findByRole("heading", { name: "Graph-Draft Batches" })).toBeInTheDocument();
    expect(screen.getAllByText("Batch drafted one question").length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText("2 notes").length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText("1 ops")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Review batch" }));
    await waitFor(() => expect(window.location.pathname).toBe(`/app/batches/${batchId}`));
  });

  it("shows the lab-wide graph-draft review queue grouped by project and submitter", async () => {
    const firstDraftId = "22222222-2222-4222-8222-222222222222";
    const secondDraftId = "33333333-3333-4333-8333-333333333333";
    localStorage.setItem(TOKEN_STORAGE_KEY, "token-review-queue");
    localStorage.setItem("lab-tracker:last-used-project-id", "project-1");
    window.history.replaceState({}, "", "/app/review");
    const submittedDrafts = [
      {
        change_set_id: firstDraftId,
        created_at: "2026-06-12T10:00:00Z",
        created_by: "user-maya",
        created_by_username: "maya",
        draft_mode: "graph_context",
        model: "fake-review-model",
        operations: [{ operation_id: "44444444-4444-4444-8444-444444444444" }],
        project_id: "project-1",
        provider: "fake",
        source_filename: "maya-note.jpg",
        status: "submitted",
        submitted_at: "2026-06-12T11:00:00Z",
        submitted_by: "user-maya",
        submitted_by_username: "maya",
        summary: "Needs PI review",
        updated_at: "2026-06-12T11:00:00Z",
      },
      {
        change_set_id: secondDraftId,
        created_at: "2026-06-12T09:00:00Z",
        created_by: "user-lee",
        created_by_username: "lee",
        draft_mode: "graph_batch",
        model: "fake-review-model",
        operations: [
          { operation_id: "55555555-5555-4555-8555-555555555555" },
          { operation_id: "66666666-6666-4666-8666-666666666666" },
        ],
        project_id: "project-2",
        provider: "fake",
        source_filename: "lee-batch.txt",
        status: "submitted",
        submitted_at: "2026-06-12T10:30:00Z",
        submitted_by: "user-lee",
        submitted_by_username: "lee",
        summary: "Cross-project note",
        updated_at: "2026-06-12T10:30:00Z",
      },
    ];

    const fetchMock = installFetchMock([
      {
        match: "/auth/me",
        response: apiResponse({ role: "admin", user_id: "admin-1", username: "sam" }),
      },
      {
        match: projectsPath,
        response: apiResponse([project("project-1", "Project One"), project("project-2", "Project Two")]),
      },
      {
        match: questionCountPath("project-1"),
        response: paged([], { limit: 1, offset: 0, total: 0 }),
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
        match: projectMembersPath("project-1"),
        response: paged([
          {
            membership_id: "membership-1",
            project_id: "project-1",
            role: "owner",
            user_id: "admin-1",
            username: "sam",
          },
        ]),
      },
      {
        match: buildApiPath("/batches", { limit: 5 }),
        response: paged([], { limit: 5, offset: 0, total: 0 }),
      },
      {
        match: buildApiPath("/graph-drafts", { status: "submitted", limit: 100 }),
        response: paged(submittedDrafts, { limit: 100, offset: 0, total: 2 }),
      },
    ]);

    render(<App />);

    expect(await screen.findByRole("heading", { name: "Lab Review Queue" })).toBeInTheDocument();
    expect(screen.getByText("Pending Review")).toBeInTheDocument();
    expect(screen.getByText("Needs PI review")).toBeInTheDocument();
    expect(screen.getByText("Cross-project note")).toBeInTheDocument();
    expect(screen.getByText("maya")).toBeInTheDocument();
    expect(screen.getByText("lee")).toBeInTheDocument();
    expect(screen.getAllByText("Project One").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Project Two").length).toBeGreaterThan(0);
    expect(requestedUrls(fetchMock)).toContain(
      buildApiPath("/graph-drafts", { status: "submitted", limit: 100 })
    );

    fireEvent.click(screen.getAllByRole("button", { name: "Review draft" })[0]);
    await waitFor(() => expect(window.location.pathname).toBe(`/app/graph-drafts/${firstDraftId}`));
  });

  it("captures a mobile image with context and starts a graph-aware draft", async () => {
    const noteId = "11111111-1111-4111-8111-111111111111";
    const draftId = "22222222-2222-4222-8222-222222222222";
    localStorage.setItem(TOKEN_STORAGE_KEY, "token-mobile-capture");
    window.history.replaceState({}, "", "/app/capture");

    installFetchMock([
      {
        match: "/auth/me",
        response: apiResponse({ role: "admin", username: "sam" }),
      },
      {
        match: projectsPath,
        response: apiResponse([project("project-1", "Project One")]),
      },
      {
        match: questionListPath("project-1"),
        response: paged([
          question({
            questionId: "question-1",
            status: "active",
            text: "Can flies climb temporal odor gradients?",
          }),
        ]),
      },
      {
        match: datasetListPath("project-1"),
        response: paged([dataset({ datasetId: "dataset-1", commitHash: "dataset-hash" })]),
      },
      {
        match: noteCountPath("project-1"),
        response: [
          paged([], { limit: 1, offset: 0, total: 0 }),
          paged([], { limit: 1, offset: 0, total: 1 }),
          paged([], { limit: 1, offset: 0, total: 1 }),
        ],
      },
      {
        match: activeSessionsPath("project-1"),
        response: paged([
          session({
            primaryQuestionId: "question-1",
            sessionId: "session-1",
          }),
        ]),
      },
      {
        match: buildApiPath("/graph-drafts", { project_id: "project-1", limit: 10 }),
        response: paged([]),
      },
      {
        match: buildApiPath("/notes", { project_id: "project-1", limit: 10 }),
        response: paged([]),
      },
      {
        match: captureAnalysesPath("project-1"),
        response: paged([]),
      },
      {
        match: captureClaimsPath("project-1"),
        response: paged([]),
      },
      {
        match: "/notes/upload-file",
        method: "POST",
        response: (request) => {
          const body = request.init.body;
          expect(body.get("project_id")).toBe("project-1");
          expect(body.get("file").name).toBe("phone-capture.jpg");
          expect(JSON.parse(body.get("metadata"))).toEqual({
            capture_hint: "Rig 2 Fly 12",
            capture_kind: "image",
            capture_mode: "photo",
            capture_review_status: "draft_requested",
            capture_source: "mobile_capture",
            source_file_last_modified_at: "2026-02-01T00:00:00.000Z",
            source_file_last_modified_ms: 1769904000000,
          });
          expect(JSON.parse(body.get("targets"))).toEqual([
            { entity_id: "question-1", entity_type: "question" },
            { entity_id: "session-1", entity_type: "session" },
            { entity_id: "dataset-1", entity_type: "dataset" },
          ]);
          return apiResponse(
            note({
              noteId,
              rawAsset: {
                checksum: "abc",
                content_type: "image/jpeg",
                filename: "phone-capture.jpg",
                size_bytes: 12,
                storage_id: "33333333-3333-4333-8333-333333333333",
              },
            }),
            201
          );
        },
      },
      {
        match: questionCountPath("project-1"),
        response: [
          paged([], { limit: 1, offset: 0, total: 1 }),
          paged([], { limit: 1, offset: 0, total: 1 }),
        ],
      },
      {
        match: datasetCountPath("project-1"),
        response: [
          paged([], { limit: 1, offset: 0, total: 1 }),
          paged([], { limit: 1, offset: 0, total: 1 }),
        ],
      },
      {
        match: recentNotesPath("project-1"),
        response: paged([note({ noteId })], { limit: 5, offset: 0, total: 1 }),
      },
      {
        match: `/notes/${noteId}/graph-drafts`,
        method: "POST",
        response: (request) => {
          expect(JSON.parse(request.init.body)).toEqual({
            mode: "graph_context",
            user_hint: "Rig 2 Fly 12",
          });
          return apiResponse(
            {
              change_set_id: draftId,
              clarification_requests: [],
              context_packet: { mode: "graph_context" },
              created_at: "2026-04-20T00:00:00Z",
              draft_mode: "graph_context",
              model: "gpt-5.4-mini",
              operations: [],
              project_id: "project-1",
              prompt_version: "image-graph-draft-v2",
              provider: "openai",
              source_content_type: "image/jpeg",
              source_filename: "phone-capture.jpg",
              source_note_id: noteId,
              status: "ready",
              summary: "Mobile capture draft",
              uncertain_fields: [],
              updated_at: "2026-04-20T00:00:00Z",
            },
            201
          );
        },
      },
      {
        match: `/graph-drafts/${draftId}`,
        response: apiResponse({
          change_set_id: draftId,
          clarification_requests: [],
          context_packet: { mode: "graph_context" },
          created_at: "2026-04-20T00:00:00Z",
          draft_mode: "graph_context",
          model: "gpt-5.4-mini",
          operations: [],
          project_id: "project-1",
          prompt_version: "image-graph-draft-v2",
          provider: "openai",
          source_content_type: "image/jpeg",
          source_filename: "phone-capture.jpg",
          source_note_id: noteId,
          status: "ready",
          summary: "Mobile capture draft",
          uncertain_fields: [],
          updated_at: "2026-04-20T00:00:00Z",
        }),
      },
      {
        match: `/notes/${noteId}/raw`,
        response: apiResponse({
          checksum: "abc",
          content_base64: "aW1n",
          content_type: "image/jpeg",
          filename: "phone-capture.jpg",
          size_bytes: 12,
          storage_id: "33333333-3333-4333-8333-333333333333",
        }),
      },
    ]);

    render(<App />);

    expect(await screen.findByRole("heading", { name: "Capture" })).toBeInTheDocument();
    await waitFor(() => expect(screen.getByLabelText("Project")).toHaveValue("project-1"));

    const file = new File(["phone-bytes"], "phone-capture.jpg", {
      lastModified: 1769904000000,
      type: "image/jpeg",
    });
    fireEvent.change(screen.getByLabelText("Photo file"), { target: { files: [file] } });
    fireEvent.change(screen.getByLabelText("Active question (optional)"), {
      target: { value: "question-1" },
    });
    fireEvent.change(screen.getByLabelText("Session (optional)"), {
      target: { value: "session-1" },
    });
    fireEvent.change(screen.getByLabelText("Dataset (optional)"), {
      target: { value: "dataset-1" },
    });
    fireEvent.change(screen.getByLabelText("Short hint (optional)"), {
      target: { value: "Rig 2 Fly 12" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Upload and draft" }));

    expect(await screen.findByRole("heading", { name: "Graph Draft Review" })).toBeInTheDocument();
    expect(await screen.findByText("Mobile capture draft")).toBeInTheDocument();
  });

  it("captures a photo plus voice bundle, transcribes voice, and drafts from the bundle", async () => {
    const imageNoteId = "11111111-1111-4111-8111-111111111111";
    const voiceNoteId = "33333333-3333-4333-8333-333333333333";
    const draftId = "22222222-2222-4222-8222-222222222222";
    let uploadCount = 0;
    let bundleId = "";
    localStorage.setItem(TOKEN_STORAGE_KEY, "token-mobile-bundle");
    window.history.replaceState({}, "", "/app/capture");

    installFetchMock([
      {
        match: "/auth/me",
        response: apiResponse({ role: "admin", username: "sam" }),
      },
      {
        match: projectsPath,
        response: apiResponse([project("project-1", "Project One")]),
      },
      {
        match: questionListPath("project-1"),
        response: paged([]),
      },
      {
        match: datasetListPath("project-1"),
        response: paged([]),
      },
      {
        match: noteCountPath("project-1"),
        response: [
          paged([], { limit: 1, offset: 0, total: 0 }),
          paged([], { limit: 1, offset: 0, total: 2 }),
        ],
      },
      {
        match: activeSessionsPath("project-1"),
        response: paged([]),
      },
      {
        match: buildApiPath("/graph-drafts", { project_id: "project-1", limit: 10 }),
        response: paged([]),
      },
      {
        match: buildApiPath("/notes", { project_id: "project-1", limit: 10 }),
        response: paged([]),
      },
      {
        match: captureAnalysesPath("project-1"),
        response: paged([analysis({ analysisId: "analysis-1", methodHash: "pulse-turning" })]),
      },
      {
        match: captureClaimsPath("project-1"),
        response: paged([claim({ claimId: "claim-1" })]),
      },
      {
        match: "/notes/upload-file",
        method: "POST",
        response: (request) => {
          uploadCount += 1;
          const body = request.init.body;
          const metadata = JSON.parse(body.get("metadata"));
          expect(body.get("project_id")).toBe("project-1");
          expect(metadata.capture_mode).toBe("bundle");
          expect(metadata.capture_review_status).toBe("draft_requested");
          if (uploadCount === 1) {
            bundleId = metadata.capture_bundle_id;
            expect(body.get("file").name).toBe("notebook.jpg");
            expect(metadata.capture_kind).toBe("image");
            return apiResponse(
              note({
                metadata,
                noteId: imageNoteId,
                rawAsset: {
                  checksum: "image-checksum",
                  content_type: "image/jpeg",
                  filename: "notebook.jpg",
                  size_bytes: 12,
                  storage_id: "44444444-4444-4444-8444-444444444444",
                },
              }),
              201
            );
          }
          expect(metadata.capture_bundle_id).toBe(bundleId);
          expect(body.get("file").name).toBe("voice.webm");
          expect(metadata.capture_kind).toBe("voice");
          expect(metadata.voice_note_type).toBe("Observation");
          expect(metadata.transcript_status).toBe("pending");
          return apiResponse(
            note({
              metadata,
              noteId: voiceNoteId,
              rawAsset: {
                checksum: "audio-checksum",
                content_type: "audio/webm",
                filename: "voice.webm",
                size_bytes: 11,
                storage_id: "55555555-5555-4555-8555-555555555555",
              },
            }),
            201
          );
        },
      },
      {
        match: questionCountPath("project-1"),
        response: paged([], { limit: 1, offset: 0, total: 0 }),
      },
      {
        match: datasetCountPath("project-1"),
        response: paged([], { limit: 1, offset: 0, total: 0 }),
      },
      {
        match: recentNotesPath("project-1"),
        response: paged([], { limit: 5, offset: 0, total: 2 }),
      },
      {
        match: `/notes/${voiceNoteId}/transcript`,
        method: "POST",
        response: (request) => {
          expect(JSON.parse(request.init.body)).toEqual({ prompt: "Rig 2 Fly 12" });
          return apiResponse(
            note({
              metadata: { capture_bundle_id: bundleId, capture_kind: "voice" },
              noteId: voiceNoteId,
              rawAsset: {
                checksum: "audio-checksum",
                content_type: "audio/webm",
                filename: "voice.webm",
                size_bytes: 11,
                storage_id: "55555555-5555-4555-8555-555555555555",
              },
              transcribedText: "Fly 12 tracked better after pulse onset.",
            })
          );
        },
      },
      {
        match: `/notes/${imageNoteId}/graph-drafts`,
        method: "POST",
        response: apiResponse(
          {
            change_set_id: draftId,
            clarification_requests: [],
            context_packet: {
              mode: "graph_context",
              source_artifacts: [
                {
                  content_type: "image/jpeg",
                  filename: "notebook.jpg",
                  note_id: imageNoteId,
                  type: "image",
                },
                {
                  content_type: "audio/webm",
                  filename: "voice.webm",
                  note_id: voiceNoteId,
                  transcript_text: "Fly 12 tracked better after pulse onset.",
                  type: "audio",
                },
              ],
            },
            created_at: "2026-04-20T00:00:00Z",
            draft_mode: "graph_context",
            model: "gpt-5.4-mini",
            operations: [],
            project_id: "project-1",
            prompt_version: "multimodal-graph-draft-v1",
            provider: "openai",
            source_content_type: "image/jpeg",
            source_filename: "notebook.jpg",
            source_note_id: imageNoteId,
            status: "ready",
            summary: "Bundle draft",
            uncertain_fields: [],
            updated_at: "2026-04-20T00:00:00Z",
          },
          201
        ),
      },
      {
        match: `/graph-drafts/${draftId}`,
        response: apiResponse({
          change_set_id: draftId,
          clarification_requests: [],
          context_packet: {
            mode: "graph_context",
            source_artifacts: [
              {
                content_type: "image/jpeg",
                filename: "notebook.jpg",
                note_id: imageNoteId,
                type: "image",
              },
              {
                content_type: "audio/webm",
                filename: "voice.webm",
                note_id: voiceNoteId,
                transcript_text: "Fly 12 tracked better after pulse onset.",
                type: "audio",
              },
            ],
          },
          created_at: "2026-04-20T00:00:00Z",
          draft_mode: "graph_context",
          model: "gpt-5.4-mini",
          operations: [],
          project_id: "project-1",
          prompt_version: "multimodal-graph-draft-v1",
          provider: "openai",
          source_content_type: "image/jpeg",
          source_filename: "notebook.jpg",
          source_note_id: imageNoteId,
          status: "ready",
          summary: "Bundle draft",
          uncertain_fields: [],
          updated_at: "2026-04-20T00:00:00Z",
        }),
      },
      {
        match: `/notes/${imageNoteId}/raw`,
        response: apiResponse({
          checksum: "image-checksum",
          content_base64: "aW1n",
          content_type: "image/jpeg",
          filename: "notebook.jpg",
          size_bytes: 12,
          storage_id: "44444444-4444-4444-8444-444444444444",
        }),
      },
    ]);

    render(<App />);

    expect(await screen.findByRole("heading", { name: "Capture" })).toBeInTheDocument();
    await waitFor(() => expect(screen.getByLabelText("Project")).toHaveValue("project-1"));

    fireEvent.click(screen.getByRole("button", { name: "Add attachment" }));
    fireEvent.click(screen.getByRole("button", { name: "Photo + voice" }));
    fireEvent.change(screen.getByLabelText("Photo file"), {
      target: { files: [new File(["image"], "notebook.jpg", { type: "image/jpeg" })] },
    });
    fireEvent.change(screen.getByLabelText("Voice recording"), {
      target: { files: [new File(["audio"], "voice.webm", { type: "audio/webm" })] },
    });
    fireEvent.change(screen.getByLabelText("Short hint (optional)"), {
      target: { value: "Rig 2 Fly 12" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Upload and draft" }));

    expect(await screen.findByRole("heading", { name: "Graph Draft Review" })).toBeInTheDocument();
    expect(await screen.findByText("Bundle draft")).toBeInTheDocument();
    expect(screen.getByText("Fly 12 tracked better after pulse onset.")).toBeInTheDocument();
    expect(uploadCount).toBe(2);
  });

  it("transcribes a deferred voice capture from pending review before drafting its bundle", async () => {
    const imageNoteId = "11111111-1111-4111-8111-111111111111";
    const voiceNoteId = "33333333-3333-4333-8333-333333333333";
    const draftId = "22222222-2222-4222-8222-222222222222";
    const bundleId = "bundle-1";
    const imageCapture = note({
      metadata: {
        capture_bundle_id: bundleId,
        capture_hint: "Rig 2 Fly 12",
        capture_kind: "image",
        capture_mode: "bundle",
        capture_review_status: "pending_review",
        capture_source: "mobile_capture",
      },
      noteId: imageNoteId,
      rawAsset: {
        checksum: "image-checksum",
        content_type: "image/jpeg",
        filename: "notebook.jpg",
        size_bytes: 12,
        storage_id: "44444444-4444-4444-8444-444444444444",
      },
      transcribedText: "",
    });
    const voiceCapture = note({
      metadata: {
        capture_bundle_id: bundleId,
        capture_kind: "voice",
        capture_mode: "bundle",
        capture_review_status: "pending_review",
        capture_source: "mobile_capture",
        transcript_status: "pending",
        voice_note_type: "Observation",
      },
      noteId: voiceNoteId,
      rawAsset: {
        checksum: "audio-checksum",
        content_type: "audio/webm",
        filename: "voice.webm",
        size_bytes: 11,
        storage_id: "55555555-5555-4555-8555-555555555555",
      },
      transcribedText: "",
    });
    const transcribedVoice = {
      ...voiceCapture,
      metadata: { ...voiceCapture.metadata, transcript_status: "ready" },
      transcribed_text: "Fly 12 tracked better after pulse onset.",
    };
    localStorage.setItem(TOKEN_STORAGE_KEY, "token-pending-voice");
    window.history.replaceState({}, "", "/app/capture");

    installFetchMock([
      {
        match: "/auth/me",
        response: apiResponse({ role: "admin", username: "sam" }),
      },
      {
        match: projectsPath,
        response: apiResponse([project("project-1", "Project One")]),
      },
      {
        match: questionListPath("project-1"),
        response: paged([]),
      },
      {
        match: datasetListPath("project-1"),
        response: paged([]),
      },
      {
        match: noteCountPath("project-1"),
        response: paged([], { limit: 1, offset: 0, total: 2 }),
      },
      {
        match: activeSessionsPath("project-1"),
        response: paged([]),
      },
      {
        match: buildApiPath("/graph-drafts", { project_id: "project-1", limit: 10 }),
        response: paged([]),
      },
      {
        match: buildApiPath("/notes", { project_id: "project-1", limit: 10 }),
        response: paged([imageCapture, voiceCapture]),
      },
      {
        match: captureAnalysesPath("project-1"),
        response: paged([]),
      },
      {
        match: captureClaimsPath("project-1"),
        response: paged([]),
      },
      {
        match: `/notes/${voiceNoteId}/transcript`,
        method: "POST",
        response: (request) => {
          expect(JSON.parse(request.init.body)).toEqual({});
          return apiResponse(transcribedVoice);
        },
      },
      {
        match: `/notes/${imageNoteId}/graph-drafts`,
        method: "POST",
        response: (request) => {
          expect(JSON.parse(request.init.body)).toEqual({
            mode: "graph_context",
            user_hint: "Rig 2 Fly 12",
          });
          return apiResponse(
            {
              change_set_id: draftId,
              clarification_requests: [],
              context_packet: {
                mode: "graph_context",
                source_artifacts: [
                  {
                    content_type: "image/jpeg",
                    filename: "notebook.jpg",
                    note_id: imageNoteId,
                    type: "image",
                  },
                  {
                    content_type: "audio/webm",
                    filename: "voice.webm",
                    note_id: voiceNoteId,
                    transcript_text: "Fly 12 tracked better after pulse onset.",
                    type: "audio",
                  },
                ],
              },
              created_at: "2026-04-20T00:00:00Z",
              draft_mode: "graph_context",
              model: "gpt-5.4-mini",
              operations: [],
              project_id: "project-1",
              prompt_version: "multimodal-graph-draft-v1",
              provider: "openai",
              source_content_type: "image/jpeg",
              source_filename: "notebook.jpg",
              source_note_id: imageNoteId,
              status: "ready",
              summary: "Pending bundle draft",
              uncertain_fields: [],
              updated_at: "2026-04-20T00:00:00Z",
            },
            201
          );
        },
      },
      {
        match: `/graph-drafts/${draftId}`,
        response: apiResponse({
          change_set_id: draftId,
          clarification_requests: [],
          context_packet: {
            mode: "graph_context",
            source_artifacts: [
              {
                content_type: "image/jpeg",
                filename: "notebook.jpg",
                note_id: imageNoteId,
                type: "image",
              },
              {
                content_type: "audio/webm",
                filename: "voice.webm",
                note_id: voiceNoteId,
                transcript_text: "Fly 12 tracked better after pulse onset.",
                type: "audio",
              },
            ],
          },
          created_at: "2026-04-20T00:00:00Z",
          draft_mode: "graph_context",
          model: "gpt-5.4-mini",
          operations: [],
          project_id: "project-1",
          prompt_version: "multimodal-graph-draft-v1",
          provider: "openai",
          source_content_type: "image/jpeg",
          source_filename: "notebook.jpg",
          source_note_id: imageNoteId,
          status: "ready",
          summary: "Pending bundle draft",
          uncertain_fields: [],
          updated_at: "2026-04-20T00:00:00Z",
        }),
      },
      {
        match: `/notes/${imageNoteId}/raw`,
        response: apiResponse({
          checksum: "image-checksum",
          content_base64: "aW1n",
          content_type: "image/jpeg",
          filename: "notebook.jpg",
          size_bytes: 12,
          storage_id: "44444444-4444-4444-8444-444444444444",
        }),
      },
    ]);

    render(<App />);

    expect(await screen.findByRole("heading", { name: "Capture" })).toBeInTheDocument();
    await waitFor(() => expect(screen.getByLabelText("Project")).toHaveValue("project-1"));
    const imageRow = (await screen.findByText("notebook.jpg")).closest(".review-queue-item");
    const voiceRow = (await screen.findByText("voice.webm")).closest(".review-queue-item");
    expect(within(imageRow).getByText("voice transcript needed")).toBeInTheDocument();
    expect(within(imageRow).getByRole("button", { name: "Draft" })).toBeDisabled();

    fireEvent.click(within(voiceRow).getByRole("button", { name: "Transcribe" }));

    expect(await screen.findByText("Voice transcript ready.")).toBeInTheDocument();
    expect(await screen.findByText("Fly 12 tracked better after pulse onset.")).toBeInTheDocument();
    const readyImageRow = screen.getByText("notebook.jpg").closest(".review-queue-item");
    expect(within(readyImageRow).getByRole("button", { name: "Draft" })).not.toBeDisabled();

    fireEvent.click(within(readyImageRow).getByRole("button", { name: "Draft" }));

    expect(await screen.findByRole("heading", { name: "Graph Draft Review" })).toBeInTheDocument();
    expect(await screen.findByText("Pending bundle draft")).toBeInTheDocument();
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
        match: projectsPath,
        response: apiResponse([project("project-1", "Project One")]),
      },
      {
        match: questionCountPath("project-1"),
        response: paged([question()], { limit: 1, offset: 0, total: 12 }),
      },
      {
        match: datasetCountPath("project-1"),
        response: paged([], { limit: 1, offset: 0, total: 4 }),
      },
      {
        match: noteCountPath("project-1"),
        response: paged([], { limit: 1, offset: 0, total: 7 }),
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
        response: paged([], { limit: 1, offset: 0, total: 4 }),
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
        match: committedAnalysesMetaPath("project-1"),
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
        response: paged([], { limit: 1, offset: 0, total: 2 }),
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
        match: committedAnalysesMetaPath("project-2"),
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
    expect(urls).toContain(committedAnalysesMetaPath("project-1"));
    expect(urls).not.toContain(buildApiPath("/notes", { project_id: "project-1", limit: 200, offset: 0 }));
    expect(urls).not.toContain(buildApiPath("/sessions", { project_id: "project-1", limit: 200, offset: 0 }));
    expect(urls.some((url) => url.startsWith("/visualizations?project_id="))).toBe(false);
  });

  it("stages and activates a question from the home route", async () => {
    localStorage.setItem(TOKEN_STORAGE_KEY, "token-question-actions");

    installFetchMock([
      {
        match: "/auth/me",
        response: apiResponse({ role: "admin", username: "sam" }),
      },
      {
        match: projectsPath,
        response: apiResponse([project("project-1", "Project One")]),
      },
      {
        match: questionListPath("project-1"),
        response: [
          paged([]),
          paged([question({ questionId: "question-1", status: "staged", text: "How stable is the rig?" })]),
          paged([question({ questionId: "question-1", status: "active", text: "How stable is the rig?" })]),
        ],
      },
      {
        match: "/questions",
        method: "POST",
        response: apiResponse(
          question({ questionId: "question-1", status: "staged", text: "How stable is the rig?" }),
          201
        ),
      },
      {
        match: "/questions/question-1",
        method: "PATCH",
        response: apiResponse(
          question({ questionId: "question-1", status: "active", text: "How stable is the rig?" })
        ),
      },
      {
        match: datasetListPath("project-1"),
        response: [paged([]), paged([]), paged([])],
      },
      {
        match: noteCountPath("project-1"),
        response: [
          paged([], { limit: 1, offset: 0, total: 0 }),
          paged([], { limit: 1, offset: 0, total: 0 }),
          paged([], { limit: 1, offset: 0, total: 0 }),
        ],
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
        match: committedAnalysesMetaPath("project-1"),
        response: paged([], { limit: 1, offset: 0, total: 0 }),
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

  it("stages a question under parents and renders the question map", async () => {
    localStorage.setItem(TOKEN_STORAGE_KEY, "token-question-hierarchy");

    const rootQuestion = question({
      questionId: "question-root",
      text: "How does odor timing shape navigation?",
    });
    const childQuestion = question({
      parentQuestionIds: ["question-root"],
      questionId: "question-child",
      text: "Which temporal odor features drive forward locomotion?",
    });
    const grandchildQuestion = question({
      parentQuestionIds: ["question-child"],
      questionId: "question-grandchild",
      status: "staged",
      text: "Which controls isolate forward locomotion?",
    });
    const multiParentQuestion = question({
      parentQuestionIds: ["question-root", "question-child"],
      questionId: "question-multi",
      text: "Which analysis controls rule out speed artifacts?",
    });

    const fetchMock = installFetchMock([
      {
        match: "/auth/me",
        response: apiResponse({ role: "admin", username: "sam" }),
      },
      {
        match: projectsPath,
        response: apiResponse([project("project-1", "Temporal odor project")]),
      },
      {
        match: questionListPath("project-1"),
        response: [
          paged([rootQuestion, childQuestion, grandchildQuestion, multiParentQuestion]),
          paged([
            rootQuestion,
            childQuestion,
            grandchildQuestion,
            multiParentQuestion,
            question({
              parentQuestionIds: ["question-root"],
              questionId: "question-new",
              status: "staged",
              text: "Which plume gaps change run length?",
            }),
          ]),
        ],
      },
      {
        match: "/questions",
        method: "POST",
        response: apiResponse(
          question({
            parentQuestionIds: ["question-root"],
            questionId: "question-new",
            status: "staged",
            text: "Which plume gaps change run length?",
          }),
          201
        ),
      },
      {
        match: datasetListPath("project-1"),
        response: [paged([]), paged([])],
      },
      {
        match: noteCountPath("project-1"),
        response: [
          paged([], { limit: 1, offset: 0, total: 0 }),
          paged([], { limit: 1, offset: 0, total: 0 }),
        ],
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
        match: committedAnalysesMetaPath("project-1"),
        response: paged([], { limit: 1, offset: 0, total: 0 }),
      },
    ]);

    render(<App />);

    const questionMap = await screen.findByTestId("question-map");
    expect(
      within(questionMap).getAllByText("How does odor timing shape navigation?").length
    ).toBeGreaterThan(0);
    expect(
      within(questionMap).getAllByText(
        "Which temporal odor features drive forward locomotion?"
      ).length
    ).toBeGreaterThan(0);
    expect(
      within(questionMap).getAllByText("Which controls isolate forward locomotion?").length
    ).toBeGreaterThan(0);
    expect(within(questionMap).getAllByText("also linked elsewhere").length).toBeGreaterThan(0);

    const parentSelect = screen.getByLabelText("Parent questions");
    Array.from(parentSelect.options).forEach((option) => {
      option.selected = option.value === "question-root";
    });
    fireEvent.change(parentSelect);
    fireEvent.change(screen.getByLabelText("Question text"), {
      target: { value: "Which plume gaps change run length?" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Stage question" }));

    expect(await screen.findByText("Question staged.")).toBeInTheDocument();
    const questionPost = fetchMock.mock.calls.find(
      ([url, init]) => url === "/questions" && init?.method === "POST"
    );
    expect(JSON.parse(questionPost[1].body)).toMatchObject({
      parent_question_ids: ["question-root"],
      text: "Which plume gaps change run length?",
    });
  });

  it("dims superseded questions in the map and keeps them out of active selectors", async () => {
    localStorage.setItem(TOKEN_STORAGE_KEY, "token-question-supersession-map");

    const replacementQuestion = question({
      questionId: "question-current",
      status: "active",
      text: "Which ATF4 comparison is testable first?",
    });
    const supersededQuestion = question({
      questionId: "question-old",
      status: "superseded",
      supersededByQuestionId: "question-current",
      text: "Does lifecycle nuance explain the ATF4 phenotype?",
    });

    installFetchMock([
      {
        match: "/auth/me",
        response: apiResponse({ role: "admin", username: "sam" }),
      },
      {
        match: projectsPath,
        response: apiResponse([project("project-1", "ATF4 arbitration")]),
      },
      {
        match: questionListPath("project-1"),
        response: paged([replacementQuestion, supersededQuestion]),
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
        match: committedAnalysesMetaPath("project-1"),
        response: paged([], { limit: 1, offset: 0, total: 0 }),
      },
    ]);

    render(<App />);

    const questionMap = await screen.findByTestId("question-map");
    const supersededNode = within(questionMap)
      .getByText("Does lifecycle nuance explain the ATF4 phenotype?")
      .closest(".question-node");
    expect(supersededNode).toHaveClass("question-node-superseded");
    expect(
      within(supersededNode).getByRole("button", {
        name: "superseded by Which ATF4 comparison is testable first?",
      })
    ).toBeInTheDocument();

    const activeSelectorLabels = ["Primary question", "Link to active question (optional)"];
    activeSelectorLabels.forEach((label) => {
      screen.getAllByLabelText(label).forEach((select) => {
        const optionValues = Array.from(select.options).map((option) => option.value);
        expect(optionValues).toContain("question-current");
        expect(optionValues).not.toContain("question-old");
      });
    });
  });

  it("submits a question refactor with selected child and note moves", async () => {
    const sourceId = "11111111-1111-4111-8111-111111111111";
    const replacementId = "22222222-2222-4222-8222-222222222222";
    const childId = "33333333-3333-4333-8333-333333333333";
    const noteId = "44444444-4444-4444-8444-444444444444";
    const sourceQuestion = question({
      hypothesis: "The old wording is too broad.",
      questionId: sourceId,
      status: "active",
      text: "Does lifecycle nuance explain the ATF4 phenotype?",
    });
    const childQuestion = question({
      parentQuestionIds: [sourceId],
      questionId: childId,
      status: "staged",
      text: "Which assay should be prioritized?",
    });
    const replacementQuestion = question({
      hypothesis: "The refined contrast can be tested first.",
      questionId: replacementId,
      questionType: "hypothesis_driven",
      status: "active",
      supersedesQuestionId: sourceId,
      text: "Which ATF4 arbitration comparison is testable first?",
    });

    localStorage.setItem(TOKEN_STORAGE_KEY, "token-question-refactor");
    window.history.replaceState({}, "", `/app/questions/${sourceId}`);

    const fetchMock = installFetchMock([
      {
        match: "/auth/me",
        response: apiResponse({ role: "admin", username: "sam" }),
      },
      {
        match: projectsPath,
        response: apiResponse([project("project-1", "ATF4 arbitration")]),
      },
      {
        match: questionCountPath("project-1"),
        response: paged([sourceQuestion], { limit: 1, offset: 0, total: 2 }),
      },
      {
        match: datasetCountPath("project-1"),
        response: paged([], { limit: 1, offset: 0, total: 0 }),
      },
      {
        match: noteCountPath("project-1"),
        response: paged([], { limit: 1, offset: 0, total: 1 }),
      },
      {
        match: `/questions/${sourceId}`,
        response: apiResponse(sourceQuestion),
      },
      {
        match: questionListPath("project-1"),
        response: [
          paged([sourceQuestion, childQuestion]),
          paged([
            question({
              hypothesis: "The old wording is too broad.",
              questionId: sourceId,
              status: "superseded",
              supersededByQuestionId: replacementId,
              text: "Does lifecycle nuance explain the ATF4 phenotype?",
            }),
            replacementQuestion,
            question({
              parentQuestionIds: [replacementId],
              questionId: childId,
              status: "staged",
              text: "Which assay should be prioritized?",
            }),
          ]),
        ],
      },
      {
        match: targetedQuestionNotesPath("project-1", sourceId),
        response: paged([
          note({
            noteId,
            rawContent: "Old wording note to move.",
            targets: [{ entity_id: sourceId, entity_type: "question" }],
            transcribedText: "",
          }),
        ]),
      },
      {
        match: questionRefactorsPath(sourceId),
        response: paged([]),
      },
      {
        match: `/questions/${sourceId}/refactor`,
        method: "POST",
        response: apiResponse(
          {
            refactor: {
              created_at: "2026-04-20T02:00:00Z",
              created_by: "sam",
              project_id: "project-1",
              reason: "Make the project framing testable.",
              refactor_id: "refactor-1",
              relationship_changes: {
                child_question_ids_reparented: [childId],
                dataset_session_analysis_claim_links_moved: false,
                note_ids_retargeted: [noteId],
              },
              replacement_question_id: replacementId,
              replacement_snapshot: {},
              source_question_id: sourceId,
              source_snapshot: {},
            },
            replacement_question: replacementQuestion,
            source_question: {
              ...sourceQuestion,
              status: "superseded",
              superseded_by_question_id: replacementId,
            },
          },
          201
        ),
      },
      {
        match: `/questions/${replacementId}`,
        response: apiResponse(replacementQuestion),
      },
      {
        match: targetedQuestionNotesPath("project-1", replacementId),
        response: paged([]),
      },
      {
        match: questionRefactorsPath(replacementId),
        response: paged([
          {
            created_at: "2026-04-20T02:00:00Z",
            created_by: "sam",
            project_id: "project-1",
            reason: "Make the project framing testable.",
            refactor_id: "refactor-1",
            relationship_changes: {},
            replacement_question_id: replacementId,
            replacement_snapshot: {},
            source_question_id: sourceId,
            source_snapshot: {},
          },
        ]),
      },
    ]);

    render(<App />);

    expect(await screen.findByRole("heading", { name: "Question Detail" })).toBeInTheDocument();
    fireEvent.click(await screen.findByRole("button", { name: "Refactor question" }));
    fireEvent.change(screen.getByLabelText("Replacement question text"), {
      target: { value: "Which ATF4 arbitration comparison is testable first?" },
    });
    fireEvent.change(screen.getByLabelText("Replacement type"), {
      target: { value: "hypothesis_driven" },
    });
    fireEvent.change(screen.getByLabelText("Replacement status"), {
      target: { value: "active" },
    });
    fireEvent.change(screen.getByLabelText("Replacement hypothesis"), {
      target: { value: "The refined contrast can be tested first." },
    });
    fireEvent.change(screen.getByLabelText("Refactor reason"), {
      target: { value: "Make the project framing testable." },
    });
    fireEvent.click(screen.getByLabelText("Which assay should be prioritized?"));
    fireEvent.click(screen.getByLabelText("Old wording note to move."));
    fireEvent.click(screen.getByRole("button", { name: "Create replacement" }));

    expect(await screen.findByText("Question refactored.")).toBeInTheDocument();
    expect(
      (await screen.findAllByText("Which ATF4 arbitration comparison is testable first?")).length
    ).toBeGreaterThan(0);
    expect(await screen.findByText("Does lifecycle nuance explain the ATF4 phenotype?")).toBeInTheDocument();
    const post = fetchMock.mock.calls.find(
      ([url, init]) => url === `/questions/${sourceId}/refactor` && init?.method === "POST"
    );
    expect(JSON.parse(post[1].body)).toEqual({
      child_question_ids_to_reparent: [childId],
      note_ids_to_retarget: [noteId],
      reason: "Make the project framing testable.",
      replacement: {
        hypothesis: "The refined contrast can be tested first.",
        parent_question_ids: [],
        question_type: "hypothesis_driven",
        status: "active",
        text: "Which ATF4 arbitration comparison is testable first?",
      },
    });
  });

  it("uploads a note file from the home route and refreshes recent notes", async () => {
    localStorage.setItem(TOKEN_STORAGE_KEY, "token-note-upload");

    const fetchMock = installFetchMock([
      {
        match: "/auth/me",
        response: apiResponse({ role: "admin", username: "sam" }),
      },
      {
        match: projectsPath,
        response: apiResponse([project("project-1", "Project One")]),
      },
      {
        match: questionListPath("project-1"),
        response: paged([question({ text: "Active question" })]),
      },
      {
        match: datasetListPath("project-1"),
        response: paged([]),
      },
      {
        match: noteCountPath("project-1"),
        response: [
          paged([], { limit: 1, offset: 0, total: 0 }),
          paged([question()], { limit: 1, offset: 0, total: 1 }),
        ],
      },
      {
        match: recentNotesPath("project-1"),
        response: [
          paged([], { limit: 5, offset: 0, total: 0 }),
          paged([note({ transcribedText: "Captured session note" })], {
            limit: 5,
            offset: 0,
            total: 1,
          }),
        ],
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
        match: committedAnalysesMetaPath("project-1"),
        response: paged([], { limit: 1, offset: 0, total: 0 }),
      },
      {
        match: questionCountPath("project-1"),
        response: paged([question()], { limit: 1, offset: 0, total: 1 }),
      },
      {
        match: datasetCountPath("project-1"),
        response: paged([], { limit: 1, offset: 0, total: 0 }),
      },
      {
        match: "/notes/upload-file",
        method: "POST",
        response: apiResponse(
          note({ transcribedText: "Captured session note" }),
          201
        ),
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

    expect(requestedUrls(fetchMock).filter((url) => url === recentNotesPath("project-1"))).toHaveLength(2);
  });

  it("starts and closes a session from the home route with the active-session loader", async () => {
    localStorage.setItem(TOKEN_STORAGE_KEY, "token-session-create");

    installFetchMock([
      {
        match: "/auth/me",
        response: apiResponse({ role: "admin", username: "sam" }),
      },
      {
        match: projectsPath,
        response: apiResponse([project("project-1", "Project One")]),
      },
      {
        match: questionListPath("project-1"),
        response: paged([
          question({
            text: "Primary question",
            updatedAt: "2026-04-20T02:00:00Z",
          }),
        ]),
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
        response: [
          paged([]),
          paged([session()]),
          paged([]),
        ],
      },
      {
        match: stagedAnalysesPath("project-1"),
        response: paged([]),
      },
      {
        match: committedAnalysesMetaPath("project-1"),
        response: paged([], { limit: 1, offset: 0, total: 0 }),
      },
      {
        match: "/sessions",
        method: "POST",
        response: apiResponse(session(), 201),
      },
      {
        match: "/sessions/session-1",
        method: "PATCH",
        response: apiResponse({
          ...session(),
          ended_at: "2026-04-20T04:00:00Z",
          status: "closed",
        }),
      },
    ]);

    render(<App />);

    expect(await screen.findByRole("heading", { name: "Sessions" })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Start session" }));

    expect(await screen.findByText("Session started.")).toBeInTheDocument();
    expect(await screen.findByText("ABC123")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Close session" }));

    expect(await screen.findByText("Session closed.")).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.queryByText("ABC123")).not.toBeInTheDocument();
    });
  });

  it("loads staged dataset files lazily from the home route", async () => {
    localStorage.setItem(TOKEN_STORAGE_KEY, "token-dataset-files");

    const fetchMock = installFetchMock([
      {
        match: "/auth/me",
        response: apiResponse({ role: "admin", username: "sam" }),
      },
      {
        match: projectsPath,
        response: apiResponse([project("project-1", "Project One")]),
      },
      {
        match: questionListPath("project-1"),
        response: paged([question({ text: "Primary question" })]),
      },
      {
        match: datasetListPath("project-1"),
        response: paged([
          dataset({
            datasetId: "dataset-1",
            status: "staged",
          }),
        ]),
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
        match: committedAnalysesMetaPath("project-1"),
        response: paged([], { limit: 1, offset: 0, total: 0 }),
      },
      {
        match: datasetFilesPath("dataset-1"),
        response: paged([
          {
            checksum: "sha256-file",
            file_id: "file-1",
            path: "staged/file-1.bin",
            size_bytes: 512,
          },
        ]),
      },
    ]);

    render(<App />);

    await screen.findByRole("heading", { name: "Dataset Queue" });

    expect(requestedUrls(fetchMock)).not.toContain(datasetFilesPath("dataset-1"));

    fireEvent.click(screen.getByRole("button", { name: "Manage files" }));

    expect(await screen.findByText("staged/file-1.bin")).toBeInTheDocument();
    expect(requestedUrls(fetchMock).filter((url) => url === datasetFilesPath("dataset-1"))).toHaveLength(1);

    fireEvent.click(screen.getByRole("button", { name: "Hide files" }));
    fireEvent.click(screen.getByRole("button", { name: "Manage files" }));

    await screen.findByText("staged/file-1.bin");
    expect(requestedUrls(fetchMock).filter((url) => url === datasetFilesPath("dataset-1"))).toHaveLength(1);
  });

  it("loads analysis visualizations lazily and keeps commit/archive actions on the home route", async () => {
    localStorage.setItem(TOKEN_STORAGE_KEY, "token-analysis-queue");

    const stagedAnalysis = analysis({
      analysisId: "analysis-staged",
      codeVersion: "sha-staged",
      status: "staged",
    });
    const committedAnalysis = analysis({
      analysisId: "analysis-committed",
      codeVersion: "sha-committed",
      status: "committed",
    });
    const committedAfterCommit = analysis({
      analysisId: "analysis-staged",
      codeVersion: "sha-staged",
      status: "committed",
      updatedAt: "2026-04-20T03:00:00Z",
      executedAt: "2026-04-20T03:00:00Z",
    });

    const fetchMock = installFetchMock([
      {
        match: "/auth/me",
        response: apiResponse({ role: "admin", username: "sam" }),
      },
      {
        match: projectsPath,
        response: apiResponse([project("project-1", "Project One")]),
      },
      {
        match: questionListPath("project-1"),
        response: paged([question({ text: "Primary question" })]),
      },
      {
        match: datasetListPath("project-1"),
        response: paged([
          dataset({
            datasetId: "dataset-1",
            commitHash: "commit-1",
            status: "committed",
          }),
        ]),
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
        response: [
          paged([stagedAnalysis]),
          paged([]),
          paged([]),
        ],
      },
      {
        match: committedAnalysesMetaPath("project-1"),
        response: [
          paged([committedAnalysis], { limit: 1, offset: 0, total: 1 }),
          paged([], { limit: 1, offset: 0, total: 2 }),
          paged([committedAfterCommit], { limit: 1, offset: 0, total: 1 }),
        ],
      },
      {
        match: committedAnalysesRecentPath("project-1", 2),
        response: paged([committedAfterCommit, committedAnalysis], {
          limit: 5,
          offset: 0,
          total: 2,
        }),
      },
      {
        match: visualizationsPath("analysis-committed"),
        response: paged([
          visualization({
            analysisId: "analysis-committed",
            filePath: "viz/analysis-committed.png",
            vizType: "heatmap",
          }),
        ]),
      },
      {
        match: "/analyses/analysis-staged/commit",
        method: "POST",
        response: apiResponse(committedAfterCommit),
      },
      {
        match: "/analyses/analysis-committed",
        method: "PATCH",
        response: apiResponse({
          ...committedAnalysis,
          status: "archived",
        }),
      },
    ]);

    render(<App />);

    expect(await screen.findByText("analysis-staged")).toBeInTheDocument();
    expect(await screen.findByText("analysis-committed")).toBeInTheDocument();
    expect(requestedUrls(fetchMock)).not.toContain(visualizationsPath("analysis-committed"));
    expect(requestedUrls(fetchMock).some((url) => url.startsWith("/visualizations?project_id="))).toBe(false);

    fireEvent.click(screen.getByRole("button", { name: "Load visualizations" }));

    expect(await screen.findByText("viz/analysis-committed.png")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Commit analysis" }));

    expect(await screen.findByText("Analysis committed.")).toBeInTheDocument();
    expect(await screen.findByText("analysis-staged")).toBeInTheDocument();

    const committedRow = screen.getByText("analysis-committed").closest("article");
    fireEvent.click(within(committedRow).getByRole("button", { name: "Archive analysis" }));

    expect(await screen.findByText("Analysis archived.")).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.queryByText("analysis-committed")).not.toBeInTheDocument();
    });
  });
});
