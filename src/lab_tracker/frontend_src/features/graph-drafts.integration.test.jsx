import * as React from "react";

import {
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";

import { App } from "../app-shell.jsx";

import { TOKEN_STORAGE_KEY } from "../shared/constants.js";

import { installFetchMock } from "../test/utils.js";

import {
  apiResponse,
  datasetCountPath,
  note,
  noteCountPath,
  paged,
  project,
  projectMembersPath,
  projectsPath,
  questionCountPath,
} from "../test/fixtures.js";

describe("App", () => {
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

    expect(await screen.findByRole("heading", { name: "Review" })).toBeInTheDocument();
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

    expect(await screen.findByRole("heading", { name: "Review" })).toBeInTheDocument();
    expect(await screen.findByText("Drafted one question from the whiteboard.")).toBeInTheDocument();
    expect(screen.getByText("Context summary")).toBeInTheDocument();
    expect(screen.getByText("~1234 bytes")).toBeInTheDocument();
    expect(screen.getByText("Source artifacts: image 1")).toBeInTheDocument();
    expect(screen.getByText("no audio source artifact was included")).toBeInTheDocument();
    expect(screen.getByText("Exact protocol name")).toBeInTheDocument();
    expect(screen.getByText("Confirm whether Fly 12 should be formalized.")).toBeInTheDocument();
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

  it("revises a graph draft with typed feedback and an attached image", async () => {
    const noteId = "11111111-1111-4111-8111-111111111111";
    const draftId = "22222222-2222-4222-8222-222222222222";
    const operationId = "33333333-3333-4333-8333-333333333333";
    const draftBase = {
      change_set_id: draftId,
      clarification_requests: [],
      context_packet: { mode: "graph_context", project: { id: "project-1", label: "Project One" } },
      created_at: "2026-04-20T00:00:00Z",
      draft_mode: "graph_context",
      model: "gpt-5.4-mini",
      operations: [
        {
          change_set_id: draftId,
          client_ref: "q1",
          confidence: 0.82,
          created_at: "2026-04-20T00:00:00Z",
          entity_type: "question",
          error_metadata: {},
          op: "create",
          operation_id: operationId,
          payload: { project_id: "project-1", question_type: "descriptive", text: "Drafted question" },
          rationale: "The whiteboard asks this explicitly.",
          result_entity_id: null,
          sequence: 1,
          semantic_type: "suggest_new_question",
          source_refs: [],
          status: "proposed",
          target_entity_id: null,
          updated_at: "2026-04-20T00:00:00Z",
        },
      ],
      project_id: "project-1",
      prompt_version: "image-graph-draft-v2",
      provider: "openai",
      source_content_type: "image/jpeg",
      source_filename: "whiteboard.jpg",
      source_note_id: noteId,
      status: "ready",
      summary: "Drafted one question from the whiteboard.",
      uncertain_fields: [],
      updated_at: "2026-04-20T00:00:00Z",
    };

    localStorage.setItem(TOKEN_STORAGE_KEY, "token-graph-draft-revise");
    window.history.replaceState({}, "", `/app/graph-drafts/${draftId}`);

    let revisePayload = null;
    installFetchMock([
      { match: "/auth/me", response: apiResponse({ role: "admin", username: "sam" }) },
      { match: projectsPath, response: apiResponse([]) },
      { match: `/graph-drafts/${draftId}`, response: apiResponse(draftBase) },
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
        match: `/graph-drafts/${draftId}/revise`,
        method: "POST",
        response: (request) => {
          revisePayload = request.init.body;
          return apiResponse({
            ...draftBase,
            summary: "Revised from feedback and attached schematic.",
          });
        },
      },
    ]);

    render(<App />);

    expect(await screen.findByRole("heading", { name: "Review" })).toBeInTheDocument();
    fireEvent.change(await screen.findByPlaceholderText(/Tell the AI how to revise/), {
      target: { value: "Use the corrected schematic." },
    });
    const file = new File(["png-bytes"], "schematic.png", { type: "image/png" });
    fireEvent.change(screen.getByLabelText("Attach image"), {
      target: { files: [file] },
    });
    expect(await screen.findByText("schematic.png")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Revise with AI" }));

    expect(await screen.findByText("Revised from feedback and attached schematic.")).toBeInTheDocument();
    expect(revisePayload).toBeInstanceOf(FormData);
    expect(revisePayload.get("feedback")).toBe("Use the corrected schematic.");
    const attached = revisePayload.getAll("attachments");
    expect(attached).toHaveLength(1);
    expect(attached[0].name).toBe("schematic.png");
  });

  it("uses the draft project membership for graph-draft commit controls", async () => {
    const draftId = "22222222-2222-4222-8222-222222222222";
    const operationId = "33333333-3333-4333-8333-333333333333";
    const draftBase = {
      change_set_id: draftId,
      clarification_requests: [],
      context_packet: { project: { id: "project-2", label: "Draft Project" } },
      created_at: "2026-04-20T00:00:00Z",
      draft_mode: "graph_context",
      model: "gpt-5.4-mini",
      operations: [
        {
          change_set_id: draftId,
          confidence: 0.82,
          created_at: "2026-04-20T00:00:00Z",
          entity_type: "question",
          error_metadata: {},
          op: "create",
          operation_id: operationId,
          payload: { project_id: "project-2", text: "Draft-owned question" },
          sequence: 1,
          semantic_type: "suggest_new_question",
          source_refs: [],
          status: "accepted",
          updated_at: "2026-04-20T00:00:00Z",
        },
      ],
      project_id: "project-2",
      prompt_version: "image-graph-draft-v2",
      provider: "openai",
      source_content_type: "text/plain",
      source_filename: "",
      source_note_id: null,
      status: "ready",
      summary: "Draft from another selected project.",
      uncertain_fields: [],
      updated_at: "2026-04-20T00:00:00Z",
    };

    localStorage.setItem(TOKEN_STORAGE_KEY, "token-graph-draft-project-access");
    window.history.replaceState({}, "", `/app/graph-drafts/${draftId}`);

    installFetchMock([
      {
        match: "/auth/me",
        response: apiResponse({
          role: "editor",
          user_id: "user-1",
          username: "sam",
        }),
      },
      {
        match: projectsPath,
        response: apiResponse([
          project("project-1", "Selected Project"),
          project("project-2", "Draft Project"),
        ]),
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
            membership_id: "membership-selected",
            role: "viewer",
            user_id: "user-1",
            username: "sam",
          },
        ]),
      },
      {
        match: `/graph-drafts/${draftId}`,
        response: apiResponse(draftBase),
      },
      {
        match: projectMembersPath("project-2"),
        response: paged([
          {
            membership_id: "membership-draft",
            role: "owner",
            user_id: "user-1",
            username: "sam",
          },
        ]),
      },
      {
        match: `/graph-drafts/${draftId}/commit`,
        method: "POST",
        response: apiResponse({
          ...draftBase,
          commit_message: "Commit draft project",
          status: "committed",
        }),
      },
    ]);

    render(<App />);

    expect(await screen.findByRole("heading", { name: "Review" })).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.getByLabelText("Commit message")).toBeEnabled();
    });
    fireEvent.change(screen.getByLabelText("Commit message"), {
      target: { value: "Commit draft project" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Commit accepted changes" }));

    expect(await screen.findByText("Graph draft committed.")).toBeInTheDocument();
  });
});
