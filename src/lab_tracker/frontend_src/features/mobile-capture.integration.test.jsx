import * as React from "react";

import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";

import { App } from "../app-shell.jsx";

import { buildApiPath } from "../shared/api.js";

import { TOKEN_STORAGE_KEY } from "../shared/constants.js";

import { errorResponse, installFetchMock } from "../test/utils.js";

import {
  activeSessionsPath,
  analysis,
  apiResponse,
  captureAnalysesPath,
  captureClaimsPath,
  claim,
  dataset,
  datasetCountPath,
  datasetListPath,
  note,
  noteCountPath,
  paged,
  project,
  projectGraph,
  projectGraphPath,
  projectsPath,
  question,
  questionCountPath,
  questionListPath,
  recentNotesPath,
  session,
} from "../test/fixtures.js";

describe("App", () => {
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
    const uploadButton = screen.getByRole("button", { name: "Save capture" });
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

  it("surfaces share-target inbox write failures on the capture route", async () => {
    localStorage.setItem(TOKEN_STORAGE_KEY, "token-share-target-error");
    window.history.replaceState({}, "", "/app/capture?from-share=error");

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
    ]);

    render(<App />);

    expect(
      await screen.findByText("Shared capture could not be saved. Open Lab Tracker and try again.")
    ).toBeInTheDocument();
    expect(window.location.search).not.toContain("from-share");
  });

  it("captures a mobile image with context as a capture-only note", async () => {
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
          paged([note({ noteId })], { limit: 1, offset: 0, total: 1 }),
          paged([note({ noteId })], { limit: 1, offset: 0, total: 1 }),
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
            capture_review_status: "pending_review",
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
          paged([question({ questionId: "question-1" })], {
            limit: 1,
            offset: 0,
            total: 1,
          }),
          paged([question({ questionId: "question-1" })], {
            limit: 1,
            offset: 0,
            total: 1,
          }),
        ],
      },
      {
        match: datasetCountPath("project-1"),
        response: [
          paged([dataset({ datasetId: "dataset-1" })], {
            limit: 1,
            offset: 0,
            total: 1,
          }),
          paged([dataset({ datasetId: "dataset-1" })], {
            limit: 1,
            offset: 0,
            total: 1,
          }),
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
    fireEvent.click(screen.getByRole("button", { name: "Save capture" }));

    expect(await screen.findByText("Capture saved for review.")).toBeInTheDocument();
  });

  it("captures a photo plus voice bundle as capture-only notes", async () => {
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
          paged([note({ noteId: imageNoteId })], { limit: 1, offset: 0, total: 2 }),
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
          expect(metadata.capture_review_status).toBe("pending_review");
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
        response: () =>
          paged(
            [
              note({
                metadata: { capture_bundle_id: bundleId, capture_kind: "image" },
                noteId: imageNoteId,
              }),
              note({
                metadata: {
                  capture_bundle_id: bundleId,
                  capture_kind: "voice",
                  transcript_status: "pending",
                },
                noteId: voiceNoteId,
                transcribedText: "",
              }),
            ],
            { limit: 5, offset: 0, total: 2 }
          ),
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
    fireEvent.click(screen.getByRole("button", { name: "Save capture" }));

    expect(await screen.findByText("Capture saved for review.")).toBeInTheDocument();
    expect(uploadCount).toBe(2);
  });

  it("captures a bundle without transcribing or drafting on the phone", async () => {
    const imageNoteId = "11111111-1111-4111-8111-111111111111";
    const voiceNoteId = "33333333-3333-4333-8333-333333333333";
    const draftId = "22222222-2222-4222-8222-222222222222";
    let uploadCount = 0;
    let transcriptCount = 0;
    let bundleId = "";
    localStorage.setItem(TOKEN_STORAGE_KEY, "token-mobile-bundle-retry");
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
          paged([note({ noteId: imageNoteId })], { limit: 1, offset: 0, total: 2 }),
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
          uploadCount += 1;
          const body = request.init.body;
          const metadata = JSON.parse(body.get("metadata"));
          expect(metadata.capture_mode).toBe("bundle");
          expect(metadata.capture_review_status).toBe("pending_review");
          if (uploadCount === 1) {
            bundleId = metadata.capture_bundle_id;
            expect(body.get("file").name).toBe("notebook.jpg");
            expect(metadata.capture_kind).toBe("image");
            return apiResponse(note({ metadata, noteId: imageNoteId }), 201);
          }
          expect(uploadCount).toBe(2);
          expect(metadata.capture_bundle_id).toBe(bundleId);
          expect(body.get("file").name).toBe("voice.webm");
          expect(metadata.capture_kind).toBe("voice");
          return apiResponse(note({ metadata, noteId: voiceNoteId }), 201);
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
        response: () =>
          paged(
            [
              note({
                metadata: { capture_bundle_id: bundleId, capture_kind: "image" },
                noteId: imageNoteId,
              }),
              note({
                metadata: {
                  capture_bundle_id: bundleId,
                  capture_kind: "voice",
                  transcript_status: "pending",
                },
                noteId: voiceNoteId,
                transcribedText: "",
              }),
            ],
            { limit: 5, offset: 0, total: 2 }
          ),
      },
      {
        match: `/notes/${voiceNoteId}/transcript`,
        method: "POST",
        response: (request) => {
          transcriptCount += 1;
          expect(JSON.parse(request.init.body)).toEqual({ prompt: "Rig 2 Fly 12" });
          if (transcriptCount === 1) {
            return errorResponse("Transcript service unavailable.", 502);
          }
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
            context_packet: { mode: "graph_context" },
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
            summary: "Bundle draft after retry",
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
          context_packet: { mode: "graph_context" },
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
          summary: "Bundle draft after retry",
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
    fireEvent.click(screen.getByRole("button", { name: "Save capture" }));

    // Phone is capture-only: both notes upload as pending_review, and no
    // transcript or graph-draft request is made from the capture path.
    expect(await screen.findByText("Capture saved for review.")).toBeInTheDocument();
    expect(uploadCount).toBe(2);
    expect(transcriptCount).toBe(0);
  });

  it("transcribes a deferred voice capture from pending review", async () => {
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
        response: paged([voiceCapture], { limit: 1, offset: 0, total: 2 }),
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

    fireEvent.click(within(voiceRow).getByRole("button", { name: "Transcribe" }));

    expect(await screen.findByText("Voice transcript ready.")).toBeInTheDocument();
    expect(await screen.findByText("Fly 12 tracked better after pulse onset.")).toBeInTheDocument();
  });
});
