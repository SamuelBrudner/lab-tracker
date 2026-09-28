import * as React from "react";

import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { KioskLaunchPanel, kioskRoute } from "./KioskLaunchPanel.jsx";

describe("kioskRoute", () => {
  it("opens the kiosk with only the ids it is given", () => {
    expect(kioskRoute()).toBe("/app/capture?kiosk=1");
    expect(kioskRoute({ projectId: "project 1" })).toBe(
      "/app/capture?kiosk=1&project_id=project+1"
    );
    expect(kioskRoute({ projectId: "project-1", sessionId: "session-1" })).toBe(
      "/app/capture?kiosk=1&project_id=project-1&session_id=session-1"
    );
  });
});

describe("KioskLaunchPanel", () => {
  it("opens the kiosk for the selected project", () => {
    const navigate = vi.fn();
    render(<KioskLaunchPanel navigate={navigate} selectedProjectId="project-1" />);

    expect(screen.getByRole("heading", { name: "Bench kiosk" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Open bench kiosk" }));

    expect(navigate).toHaveBeenCalledWith("/app/capture?kiosk=1&project_id=project-1");
  });

  it("opens the kiosk without a project when none is selected", () => {
    const navigate = vi.fn();
    render(<KioskLaunchPanel navigate={navigate} />);

    fireEvent.click(screen.getByRole("button", { name: "Open bench kiosk" }));

    expect(navigate).toHaveBeenCalledWith("/app/capture?kiosk=1");
  });
});
