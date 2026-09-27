import * as React from "react";

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { apiResponse, installFetchMock } from "../../test/utils.js";
import { CaptureHealthCard, sourceLabel } from "./CaptureHealthCard.jsx";

function coverage(overrides = {}) {
  return {
    project_id: "project-1",
    unreviewed_count: 3,
    oldest_unreviewed_at: "2026-09-03T09:00:00Z",
    unplaced_count: 0,
    archived_unreviewed_count: 0,
    pending_change_sets: 0,
    open_clarification_requests: 0,
    last_capture_at: "2026-09-23T09:00:00Z",
    capture_sources_truncated: false,
    recent_days: 7,
    quiet_window_days: 30,
    quiet_source_count: 1,
    capture_sources: [
      {
        evidence_source_provider: "local-figure",
        evidence_adapter: "lab-tracker-client-figure",
        capture_install_id: "install-a",
        capture_host_label: "rig-2",
        note_count: 3,
        last_capture_at: "2026-09-23T09:00:00Z",
        recent_note_count: 3,
        staged_unreviewed_count: 2,
        quiet: false,
      },
      {
        evidence_source_provider: "local-folder",
        evidence_adapter: "lt-watch-files",
        capture_install_id: "install-a",
        capture_host_label: "rig-2",
        note_count: 1,
        last_capture_at: "2026-09-03T09:00:00Z",
        recent_note_count: 0,
        staged_unreviewed_count: 1,
        quiet: true,
      },
    ],
    ...overrides,
  };
}

describe("CaptureHealthCard", () => {
  it("lists coverage capture sources by adapter and host and flags the quiet ones", async () => {
    installFetchMock([
      { match: "/projects/project-1/coverage", response: apiResponse(coverage()) },
    ]);
    render(<CaptureHealthCard token="token-1" projectId="project-1" />);

    expect(await screen.findByText("1 gone quiet")).toBeInTheDocument();
    expect(screen.getByText("Python figure capture on rig-2")).toBeInTheDocument();
    expect(screen.getByText("Watch folder on rig-2")).toBeInTheDocument();
    expect(screen.getByText("quiet for over 7 days")).toBeInTheDocument();
    expect(screen.getByText(/3 in the last 7 days/)).toBeInTheDocument();
    expect(screen.getByText(/2 awaiting review/)).toBeInTheDocument();
  });

  it("explains what to set up when nothing was captured", async () => {
    installFetchMock([
      {
        match: "/projects/project-1/coverage",
        response: apiResponse(coverage({ capture_sources: [], quiet_source_count: 0 })),
      },
    ]);
    render(<CaptureHealthCard token="token-1" projectId="project-1" />);

    expect(await screen.findByText("0 sources active")).toBeInTheDocument();
    expect(screen.getByText(/Nothing has been captured yet/)).toBeInTheDocument();
  });

  it("renders nothing without a project and names sources by their most specific key", () => {
    const { container } = render(<CaptureHealthCard token="token-1" projectId="" />);
    expect(container).toBeEmptyDOMElement();
    expect(sourceLabel({ evidence_adapter: "custom-adapter" })).toBe("custom-adapter");
    expect(sourceLabel({ evidence_adapter: "mobile_capture" })).toBe("Phone capture");
    expect(sourceLabel({ evidence_source_provider: "git" })).toBe("git");
    expect(sourceLabel({})).toBe("Typed in the app");
  });
});
