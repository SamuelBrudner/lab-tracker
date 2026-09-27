import * as React from "react";

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { apiResponse, errorResponse, installFetchMock } from "../../test/utils.js";
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
    server_release: { version: "0.5.0", revision: null },
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
        capture_client_version: "0.5.0",
        release_status: "current",
        update_notice: null,
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
        capture_client_version: "0.3.0",
        release_status: "behind",
        update_notice:
          "lab-tracker on the machine watching `fly_walking_data` (rig-2) is behind this server.",
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

  it("marks a source whose client is behind with a pill carrying its notice", async () => {
    installFetchMock([
      { match: "/projects/project-1/coverage", response: apiResponse(coverage()) },
    ]);
    render(<CaptureHealthCard token="token-1" projectId="project-1" />);

    const pills = await screen.findAllByText("client behind");
    expect(pills).toHaveLength(1);
    const notice =
      "lab-tracker on the machine watching `fly_walking_data` (rig-2) is behind this server.";
    expect(pills[0]).toHaveAttribute("title", notice);
    expect(pills[0]).toHaveAttribute("aria-label", `client behind: ${notice}`);
    // The quiet watcher and its behind client are one row, so the two are read together.
    const row = pills[0].closest("li");
    expect(row).toHaveTextContent("Watch folder on rig-2");
    expect(row).toHaveTextContent("quiet for over 7 days");
  });

  it("describes a behind client without a notice by its release", async () => {
    const [current, quiet] = coverage().capture_sources;
    installFetchMock([
      {
        match: "/projects/project-1/coverage",
        response: apiResponse(
          coverage({ capture_sources: [current, { ...quiet, update_notice: null }] })
        ),
      },
    ]);
    render(<CaptureHealthCard token="token-1" projectId="project-1" />);

    const pill = await screen.findByText("client behind");
    expect(pill).toHaveAttribute(
      "title",
      "Captured with release 0.3.0; this server runs release 0.5.0."
    );
  });

  it("says capture health is unavailable when the coverage read fails", async () => {
    installFetchMock([
      {
        match: "/projects/project-1/coverage",
        response: errorResponse("Coverage is down.", 500),
      },
    ]);
    render(<CaptureHealthCard token="token-1" projectId="project-1" />);

    expect(await screen.findByText(/Capture health is unavailable\./)).toBeInTheDocument();
    expect(screen.queryByText("client behind")).not.toBeInTheDocument();
  });

  it("notes a truncated listing while the quiet count still covers every source", async () => {
    installFetchMock([
      {
        match: "/projects/project-1/coverage",
        response: apiResponse(coverage({ capture_sources_truncated: true, quiet_source_count: 3 })),
      },
    ]);
    render(<CaptureHealthCard token="token-1" projectId="project-1" />);

    expect(await screen.findByText("3 gone quiet")).toBeInTheDocument();
    expect(
      screen.getByText(
        "Only the most recent sources are listed; the quiet count covers every source."
      )
    ).toBeInTheDocument();
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
    expect(screen.queryByText(/phone/i)).not.toBeInTheDocument();
  });

  it("renders nothing without a project and names sources by their most specific key", () => {
    const { container } = render(<CaptureHealthCard token="token-1" projectId="" />);
    expect(container).toBeEmptyDOMElement();
    expect(sourceLabel({ evidence_adapter: "custom-adapter" })).toBe("custom-adapter");
    expect(sourceLabel({ evidence_source_provider: "git" })).toBe("git");
    // Phone and share-sheet captures carry no adapter, so they share the
    // unattributed bucket with typed notes, which nothing monitors.
    expect(sourceLabel({})).toBe("Typed or phone captures (not monitored)");
  });
});
