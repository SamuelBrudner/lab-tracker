import * as React from "react";

import {
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";

import { App } from "../app-shell.jsx";

import { buildApiPath } from "../shared/api.js";

import { TOKEN_STORAGE_KEY } from "../shared/constants.js";

import { installFetchMock } from "../test/utils.js";

import {
  activeSessionsPath,
  apiResponse,
  datasetCountPath,
  datasetListPath,
  note,
  noteCountPath,
  paged,
  project,
  projectMembersPath,
  projectsPath,
  questionCountPath,
  questionListPath,
  requestedUrls,
} from "../test/fixtures.js";

describe("App", () => {
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
        response: paged([note()], { limit: 1, offset: 0, total: 2 }),
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
      {
        match: `/graph-drafts/${batchId}`,
        response: apiResponse(pendingBatch),
      },
    ]);

    render(<App />);

    expect(await screen.findByText("1 daily review ready")).toBeInTheDocument();
    expect(await screen.findByRole("heading", { name: "Daily review" })).toBeInTheDocument();
    // The queue list renders from its own fetch after the badge/heading land,
    // so these lookups must retry (findBy*) rather than race it (getBy*).
    expect((await screen.findAllByText("Batch drafted one question")).length).toBeGreaterThanOrEqual(
      1,
    );
    expect((await screen.findAllByText("2 notes")).length).toBeGreaterThanOrEqual(1);
    expect(await screen.findByText("1 ops")).toBeInTheDocument();
    fireEvent.click(await screen.findByRole("button", { name: "Review batch" }));
    await waitFor(() => expect(window.location.pathname).toBe(`/app/batches/${batchId}`));
    expect(await screen.findByRole("heading", { name: "Listen & respond" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Dashboard" })).not.toBeInTheDocument();
    expect(document.querySelector(".review-app-shell")).toBeInTheDocument();
  });

  it("does not expose the lab-wide graph-draft review queue route", async () => {
    localStorage.setItem(TOKEN_STORAGE_KEY, "token-review-queue");
    localStorage.setItem("lab-tracker:last-used-project-id", "project-1");
    window.history.replaceState({}, "", "/app/review");

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
    ]);

    render(<App />);

    expect(await screen.findByRole("heading", { name: "Unknown View" })).toBeInTheDocument();
    expect(screen.getByText("No route matches: /app/review")).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Lab Review Queue" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Review queue" })).not.toBeInTheDocument();
    expect(requestedUrls(fetchMock)).not.toContain(
      buildApiPath("/graph-drafts", { status: "submitted", limit: 100 })
    );
  });
});
