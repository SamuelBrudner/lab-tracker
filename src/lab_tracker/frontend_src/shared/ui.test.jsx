import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { AppNavigation, UpdateAvailableBanner, WorkflowCoverageCard } from "./ui.jsx";

describe("UpdateAvailableBanner", () => {
  it("prompts for a deliberate reload when an update is ready", () => {
    const onReload = vi.fn();

    render(<UpdateAvailableBanner onReload={onReload} />);

    expect(screen.getByRole("status")).toHaveTextContent(
      "An updated version of Lab Tracker is ready."
    );
    fireEvent.click(screen.getByRole("button", { name: "Reload to update" }));

    expect(onReload).toHaveBeenCalledTimes(1);
  });

  it("stays hidden when no update is ready", () => {
    const { container } = render(<UpdateAvailableBanner />);

    expect(container).toBeEmptyDOMElement();
  });
});

function primaryLabels(nav) {
  return Array.from(nav.children)
    .filter((element) => element.tagName === "BUTTON")
    .map((element) => element.textContent);
}

function settingsGroup(container) {
  const group = container.querySelector("details.app-nav-group");
  expect(group).not.toBeNull();
  return group;
}

function settingsLabels(group) {
  return Array.from(group.querySelectorAll("button")).map((element) => element.textContent);
}

describe("AppNavigation", () => {
  it("shows Home, Capture, Review, Graph first and groups settings pages", () => {
    const { container } = render(<AppNavigation activeKind="home" navigate={vi.fn()} />);

    const nav = screen.getByRole("navigation", { name: "Primary" });
    expect(primaryLabels(nav)).toEqual(["Home", "Capture", "Review", "Graph"]);
    const group = settingsGroup(container);
    expect(group.querySelector("summary")).toHaveTextContent("Settings");
    expect(group.open).toBe(false);
    expect(settingsLabels(group)).toEqual(["Devices", "Agents", "Setup"]);
    expect(screen.getByRole("button", { name: "Home" })).toHaveClass("active");
  });

  it("adds Users to the Settings group only for admins", () => {
    const { container } = render(<AppNavigation activeKind="home" isAdmin navigate={vi.fn()} />);

    expect(settingsLabels(settingsGroup(container))).toEqual([
      "Devices",
      "Agents",
      "Setup",
      "Users",
    ]);
  });

  it("navigates to /app/batches and /app/graph", () => {
    const navigate = vi.fn();
    render(<AppNavigation activeKind="home" navigate={navigate} />);

    fireEvent.click(screen.getByRole("button", { name: "Review" }));
    fireEvent.click(screen.getByRole("button", { name: "Graph" }));

    expect(navigate.mock.calls).toEqual([["/app/batches"], ["/app/graph"]]);
  });

  it("marks Review active on batch and graph-draft routes", () => {
    for (const kind of ["batches", "batch", "graph-draft"]) {
      const { unmount } = render(<AppNavigation activeKind={kind} navigate={vi.fn()} />);
      expect(screen.getByRole("button", { name: "Review" })).toHaveClass("active");
      expect(screen.getByRole("button", { name: "Graph" })).not.toHaveClass("active");
      unmount();
    }

    render(<AppNavigation activeKind="graph" navigate={vi.fn()} />);
    expect(screen.getByRole("button", { name: "Graph" })).toHaveClass("active");
    expect(screen.getByRole("button", { name: "Review" })).not.toHaveClass("active");
  });

  it("opens the Settings group when a settings page is active", () => {
    const { container } = render(<AppNavigation activeKind="devices" navigate={vi.fn()} />);

    const group = settingsGroup(container);
    expect(group.open).toBe(true);
    expect(group.querySelector("summary")).toHaveClass("active");
    expect(screen.getByRole("button", { name: "Devices" })).toHaveClass("active");
    expect(screen.getByRole("button", { name: "Home" })).not.toHaveClass("active");
  });
});

describe("WorkflowCoverageCard", () => {
  it("names capture, review and recall in order", () => {
    render(<WorkflowCoverageCard />);

    expect(
      screen.getByRole("heading", { name: "Capture, review, recall" })
    ).toBeInTheDocument();
    const items = screen.getAllByText(/^[123]\. /).map((element) => element.textContent);
    expect(items).toHaveLength(3);
    expect(items[0]).toMatch(/^1\. Capture/);
    expect(items[1]).toMatch(/^2\. Review/);
    expect(items[2]).toMatch(/^3\. Recall/);
    expect(items[1]).toContain("only you commit");
  });
});
