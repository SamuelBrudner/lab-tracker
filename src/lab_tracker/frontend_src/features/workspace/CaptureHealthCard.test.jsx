import * as React from "react";

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { apiResponse, installFetchMock } from "../../test/utils.js";
import { CaptureHealthCard, adapterLabel } from "./CaptureHealthCard.jsx";

function report(overrides = {}) {
  return {
    project_id: "project-1",
    generated_at: "2026-09-23T12:00:00Z",
    window_days: 30,
    recent_days: 7,
    captured_window: 4,
    staged_unreviewed: 3,
    quiet_sources: 1,
    sources: [
      {
        adapter: "lab-tracker-client-figure",
        host_label: "rig-2",
        last_captured_at: "2026-09-23T09:00:00Z",
        captured_recent: 3,
        captured_window: 3,
        staged_unreviewed: 2,
        quiet: false,
      },
      {
        adapter: "lt-watch-files",
        host_label: "rig-2",
        last_captured_at: "2026-09-03T09:00:00Z",
        captured_recent: 0,
        captured_window: 1,
        staged_unreviewed: 1,
        quiet: true,
      },
    ],
    ...overrides,
  };
}

describe("CaptureHealthCard", () => {
  it("lists capture paths by adapter and host and flags the ones that went quiet", async () => {
    installFetchMock([
      { match: "/projects/project-1/capture-health", response: apiResponse(report()) },
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
        match: "/projects/project-1/capture-health",
        response: apiResponse(report({ sources: [], captured_window: 0, quiet_sources: 0 })),
      },
    ]);
    render(<CaptureHealthCard token="token-1" projectId="project-1" />);

    expect(await screen.findByText("0 sources active")).toBeInTheDocument();
    expect(screen.getByText(/Nothing was captured in the last 30 days/)).toBeInTheDocument();
  });

  it("renders nothing without a project and maps unknown adapters to their raw name", () => {
    const { container } = render(<CaptureHealthCard token="token-1" projectId="" />);
    expect(container).toBeEmptyDOMElement();
    expect(adapterLabel("custom-adapter")).toBe("custom-adapter");
    expect(adapterLabel("mobile_capture")).toBe("Phone capture");
  });
});
