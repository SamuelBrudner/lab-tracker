import * as React from "react";

import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";

import { App } from "./app-shell.jsx";
import { buildApiPath } from "./shared/api.js";
import { TOKEN_STORAGE_KEY } from "./shared/constants.js";
import { apiResponse, errorResponse, installFetchMock } from "./test/utils.js";

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

function paged(data, { limit = 200, offset = 0, total = data.length } = {}) {
  return apiResponse(data, 200, { limit, offset, total });
}

function project(projectId, name) {
  return { name, project_id: projectId };
}

function question({
  createdAt = "2026-04-20T00:00:00Z",
  projectId = "project-1",
  questionId = "question-1",
  status = "active",
  text = "Question",
  updatedAt = "2026-04-20T01:00:00Z",
} = {}) {
  return {
    created_at: createdAt,
    project_id: projectId,
    question_id: questionId,
    question_type: "descriptive",
    status,
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

function requestedUrls(fetchMock) {
  return fetchMock.mock.calls.map(([input]) => (typeof input === "string" ? input : input.url));
}

describe("App", () => {
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
    ]);

    render(<App />);

    expect(await screen.findByRole("heading", { name: "Question Detail" })).toBeInTheDocument();
    expect(await screen.findByText("How stable is the rig today?")).toBeInTheDocument();
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
          created_at: "2026-04-20T00:00:00Z",
          model: "gpt-5.4-mini",
          operations: [],
          project_id: "project-1",
          prompt_version: "image-graph-draft-v1",
          provider: "openai",
          source_content_type: "image/jpeg",
          source_note_id: noteId,
          status: "ready",
          updated_at: "2026-04-20T00:00:00Z",
        }),
      },
      {
        match: `/graph-drafts/${draftId}`,
        response: apiResponse({
          change_set_id: draftId,
          created_at: "2026-04-20T00:00:00Z",
          model: "gpt-5.4-mini",
          operations: [],
          project_id: "project-1",
          prompt_version: "image-graph-draft-v1",
          provider: "openai",
          source_content_type: "image/jpeg",
          source_note_id: noteId,
          status: "ready",
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
      source_refs: [{ label: "whiteboard", quote: "yield?", region: null }],
      status: "proposed",
      target_entity_id: null,
      updated_at: "2026-04-20T00:00:00Z",
    };
    const draftBase = {
      change_set_id: draftId,
      created_at: "2026-04-20T00:00:00Z",
      model: "gpt-5.4-mini",
      operations: [baseOperation],
      project_id: "project-1",
      prompt_version: "image-graph-draft-v1",
      provider: "openai",
      source_content_type: "image/jpeg",
      source_filename: "whiteboard.jpg",
      source_note_id: noteId,
      status: "ready",
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
    fireEvent.change(screen.getByLabelText("Payload JSON"), {
      target: {
        value: JSON.stringify({
          project_id: "project-1",
          question_type: "descriptive",
          text: "Edited draft question",
        }),
      },
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
