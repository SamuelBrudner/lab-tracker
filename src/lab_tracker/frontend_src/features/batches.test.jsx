import * as React from "react";

import { fireEvent, render, screen } from "@testing-library/react";
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

describe("BatchReviewPage scheduled indicator", () => {
  it("labels only the pending batch whose run was scheduled", async () => {
    installFetchMock([
      {
        match: /^\/batches\?project_id=p1/,
        response: apiResponse([
          {
            change_set_id: "cs-sched",
            status: "ready",
            summary: "Scheduled batch",
            source_note_count: 1,
            operations: [],
            created_at: "2026-06-25T00:00:00Z",
          },
          {
            change_set_id: "cs-uncorrelated",
            status: "ready",
            summary: "Uncorrelated batch",
            source_note_count: 1,
            operations: [],
            created_at: "2026-06-25T00:00:00Z",
          },
        ]),
      },
      {
        match: /^\/batches\/runs\?/,
        response: apiResponse([
          {
            run_id: "r1",
            change_set_id: "cs-sched",
            trigger: "scheduled",
            status: "ready",
            note_count: 1,
            window_end: "2026-06-25T00:00:00Z",
          },
        ]),
      },
      {
        match: /graph-draft-batch-settings$/,
        response: apiResponse({
          enabled: true,
          cadence_minutes: 1440,
          run_at_local_time: "18:00",
          timezone_name: "UTC",
          next_run_at: null,
        }),
      },
    ]);

    render(
      <BatchReviewPage
        token="token-1"
        projects={[{ project_id: "p1", name: "Project One" }]}
        selectedProjectId="p1"
        onSelectedProjectChange={vi.fn()}
        navigate={vi.fn()}
        canManageGraph
        setBusy={vi.fn()}
        setFlash={vi.fn()}
      />
    );

    // Both batches render, but only the one correlated to a scheduled run is labeled.
    expect(await screen.findByText("Uncorrelated batch")).toBeInTheDocument();
    expect(screen.getAllByTitle(/scheduled daily review/i)).toHaveLength(1);
  });
});
