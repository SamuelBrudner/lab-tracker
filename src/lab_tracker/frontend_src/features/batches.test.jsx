import * as React from "react";

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { vi } from "vitest";

import { BatchReviewPage, PendingBatchBanner } from "./batches.jsx";
import { apiResponse, installFetchMock } from "../test/utils.js";

describe("PendingBatchBanner", () => {
  it("nudges the user to flesh out a meeting when a pending batch has meeting notes", async () => {
    installFetchMock([
      {
        match: "/batches?limit=5",
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
        match: "/batches?limit=5",
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
        match: "/batches?limit=5",
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
  it("saves cadence as the project settings consumed by run-due", async () => {
    let settingsBody = null;
    const fetchMock = installFetchMock([
      {
        match: "/batches?project_id=project-1&limit=100",
        response: apiResponse([], 200, { limit: 100, offset: 0, total: 0 }),
      },
      {
        match: "/batches/runs?project_id=project-1&limit=20",
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
        setBusy={vi.fn()}
        setFlash={vi.fn()}
      />
    );

    expect(await screen.findByRole("heading", { name: "Cadence" })).toBeInTheDocument();
    fireEvent.click(screen.getByLabelText("Enabled"));
    fireEvent.change(screen.getByLabelText("Cadence"), { target: { value: "1440" } });
    fireEvent.change(screen.getByLabelText("Local run time"), { target: { value: "18:00" } });
    fireEvent.click(screen.getByRole("button", { name: "Save cadence" }));

    await waitFor(() => {
      expect(settingsBody).toEqual({
        cadence_minutes: 1440,
        enabled: true,
        run_at_local_time: "18:00",
        timezone_name: "America/New_York",
      });
    });
    expect(fetchMock).toHaveBeenCalled();
  });
});
