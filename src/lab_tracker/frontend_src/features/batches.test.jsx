import * as React from "react";

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { vi } from "vitest";

import { BatchReviewPage, PendingBatchBanner } from "./batches.jsx";
import { apiResponse, installFetchMock } from "../test/utils.js";

describe("PendingBatchBanner", () => {
  it("nudges the user to flesh out a meeting when a pending batch has meeting notes", async () => {
    installFetchMock([
      {
        match: "/batches?limit=5&mine=true",
        response: apiResponse([
          {
            change_set_id: "cs-meeting",
            status: "ready",
            source_note_count: 2,
            meeting_note_count: 1,
          },
        ]),
      },
    ]);

    render(<PendingBatchBanner token="token-1" navigate={vi.fn()} />);

    expect(
      await screen.findByText(/a meeting is waiting to be fleshed out/i)
    ).toBeInTheDocument();
  });

  it("shows the plain batch count when no meeting notes are present", async () => {
    installFetchMock([
      {
        match: "/batches?limit=5&mine=true",
        response: apiResponse([
          {
            change_set_id: "cs-1",
            status: "ready",
            source_note_count: 1,
            meeting_note_count: 0,
          },
        ]),
      },
    ]);

    render(<PendingBatchBanner token="token-1" navigate={vi.fn()} />);

    expect(await screen.findByText("1 daily review ready")).toBeInTheDocument();
  });

  it("deep-links Review to the meeting batch even when it is not first", async () => {
    const navigate = vi.fn();
    installFetchMock([
      {
        match: "/batches?limit=5&mine=true",
        response: apiResponse([
          {
            change_set_id: "cs-plain",
            status: "ready",
            source_note_count: 1,
            meeting_note_count: 0,
          },
          {
            change_set_id: "cs-meeting",
            status: "ready",
            source_note_count: 1,
            meeting_note_count: 2,
          },
        ]),
      },
    ]);

    render(<PendingBatchBanner token="token-1" navigate={navigate} />);

    fireEvent.click(await screen.findByRole("button", { name: "Review" }));
    expect(navigate).toHaveBeenCalledWith("/app/batches/cs-meeting");
  });
});

describe("BatchReviewPage", () => {
  it("shows distinct personal, waiting, and owner-commit queues", async () => {
    installFetchMock([
      {
        match: "/batches?project_id=project-1&mine=true&limit=100",
        response: apiResponse([
          {
            change_set_id: "ready-1",
            created_at: "2026-07-16T12:00:00Z",
            operations: [],
            source_note_count: 1,
            status: "ready",
            summary: "Ready for this reviewer",
          },
        ]),
      },
      {
        match: "/batches?project_id=project-1&mine=true&status=submitted&limit=100",
        response: apiResponse([
          {
            change_set_id: "waiting-1",
            created_at: "2026-07-16T12:00:00Z",
            operations: [],
            source_note_count: 1,
            status: "submitted",
            summary: "Waiting for project review",
          },
          {
            change_set_id: "commit-1",
            created_at: "2026-07-16T12:00:00Z",
            operations: [],
            source_note_count: 1,
            status: "submitted",
            summary: "Needs owner commit",
          },
        ]),
      },
      {
        match: "/batches?project_id=project-1&needs_commit=true&limit=100",
        response: apiResponse([
          {
            change_set_id: "commit-1",
            created_at: "2026-07-16T12:00:00Z",
            operations: [],
            source_note_count: 1,
            status: "submitted",
            summary: "Needs owner commit",
          },
        ]),
      },
      {
        match:
          "/batches?project_id=project-1&unassigned_oversight=true&limit=100",
        response: apiResponse([
          {
            change_set_id: "legacy-1",
            created_at: "2026-07-16T12:00:00Z",
            operations: [],
            source_note_count: 1,
            status: "changes_requested",
            summary: "Legacy unassigned review",
          },
        ]),
      },
      {
        match: "/batches/runs?project_id=project-1&mine=true&limit=20",
        response: apiResponse([]),
      },
      {
        match: "/projects/project-1/graph-draft-batch-settings",
        response: apiResponse({
          cadence_minutes: 1440,
          email_notifications_enabled: false,
          enabled: false,
          next_run_at: null,
          notification_email: null,
          project_id: "project-1",
          run_at_local_time: "18:00",
          settings_id: "settings-1",
          timezone_name: "America/New_York",
          user_id: "reviewer-1",
        }),
      },
    ]);

    render(
      <BatchReviewPage
        token="token-1"
        projects={[{ name: "Project One", project_id: "project-1" }]}
        selectedProjectId="project-1"
        onSelectedProjectChange={vi.fn()}
        navigate={vi.fn()}
        canManageGraph={true}
        canManageProject={true}
        setBusy={vi.fn()}
        setFlash={vi.fn()}
      />
    );

    expect(await screen.findByText("Ready for this reviewer")).toBeInTheDocument();
    expect(screen.getByText("Waiting for project review")).toBeInTheDocument();
    expect(screen.getAllByText("Needs owner commit")).toHaveLength(1);
    expect(screen.getByText("Legacy unassigned review")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Ready for you" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Waiting on others" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Needs commit" })).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { name: "Unassigned project oversight" })
    ).toBeInTheDocument();
  });

  it("saves cadence as the reviewer's personal run-due settings", async () => {
    let settingsBody = null;
    const fetchMock = installFetchMock([
      {
        match: "/batches?project_id=project-1&mine=true&limit=100",
        response: apiResponse([], 200, { limit: 100, offset: 0, total: 0 }),
      },
      {
        match: "/batches?project_id=project-1&mine=true&status=submitted&limit=100",
        response: apiResponse([], 200, { limit: 100, offset: 0, total: 0 }),
      },
      {
        match: "/batches?project_id=project-1&needs_commit=true&limit=100",
        response: apiResponse([], 200, { limit: 100, offset: 0, total: 0 }),
      },
      {
        match:
          "/batches?project_id=project-1&unassigned_oversight=true&limit=100",
        response: apiResponse([], 200, { limit: 100, offset: 0, total: 0 }),
      },
      {
        match: "/batches/runs?project_id=project-1&mine=true&limit=20",
        response: apiResponse([], 200, { limit: 20, offset: 0, total: 0 }),
      },
      {
        match: "/projects/project-1/graph-draft-batch-settings",
        response: [
          apiResponse({
            cadence_minutes: 720,
            enabled: false,
            next_run_at: null,
            project_id: "project-1",
            run_at_local_time: "06:00",
            settings_id: "settings-1",
            timezone_name: "America/New_York",
          }),
          apiResponse({
            cadence_minutes: 1440,
            enabled: true,
            next_run_at: "2026-06-25T22:00:00Z",
            project_id: "project-1",
            run_at_local_time: "18:00",
            settings_id: "settings-1",
            timezone_name: "America/New_York",
          }),
        ],
      },
      {
        match: "/projects/project-1/graph-draft-batch-settings",
        method: "PATCH",
        response: (request) => {
          settingsBody = JSON.parse(request.init.body);
          return apiResponse({
            ...settingsBody,
            next_run_at: "2026-06-25T22:00:00Z",
            project_id: "project-1",
            settings_id: "settings-1",
          });
        },
      },
    ]);

    render(
      <BatchReviewPage
        token="token-1"
        projects={[{ name: "Project One", project_id: "project-1" }]}
        selectedProjectId="project-1"
        onSelectedProjectChange={vi.fn()}
        navigate={vi.fn()}
        canManageGraph={true}
        canManageProject={true}
        setBusy={vi.fn()}
        setFlash={vi.fn()}
      />
    );

    expect(await screen.findByRole("heading", { name: "Your cadence" })).toBeInTheDocument();
    // Wait for the settings GET to populate the form before interacting. The
    // "Enabled" checkbox defaults to checked but loads unchecked, so clicking
    // before the async load resolves lets the load clobber the toggle (the
    // request would then carry enabled: false and the save assertion flakes).
    const enabledCheckbox = screen.getByLabelText("Enabled");
    await waitFor(() => expect(enabledCheckbox).not.toBeChecked());
    fireEvent.click(enabledCheckbox);
    fireEvent.change(screen.getByLabelText("Cadence"), { target: { value: "1440" } });
    fireEvent.change(screen.getByLabelText("Local run time"), { target: { value: "18:00" } });
    fireEvent.click(screen.getByRole("button", { name: "Save cadence" }));

    await waitFor(() => {
      expect(settingsBody).toEqual({
        cadence_minutes: 1440,
        email_notifications_enabled: false,
        enabled: true,
        notification_email: null,
        run_at_local_time: "18:00",
        timezone_name: "America/New_York",
      });
    });
    expect(fetchMock).toHaveBeenCalled();
  });
});
