import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  AppLink,
  appBasePath,
  isContextualProjectReady,
  parseAppRoute,
  resolveAppPath,
} from "./routing.jsx";

const QUESTION_ID = "fb3454e0-6319-40bb-864c-9de91d0b04f1";
const PROJECT_ID = "0d637c19-0060-4dc7-8304-ef7c8c940d85";

describe("app routing", () => {
  it("parses the agent-access page route", () => {
    expect(parseAppRoute("/app/agents")).toEqual({ kind: "agents" });
    expect(parseAppRoute("/lab-tracker/app/agents")).toEqual({ kind: "agents" });
  });

  it("parses the guided setup route at root and under a deployment prefix", () => {
    expect(parseAppRoute("/app/setup")).toEqual({ kind: "setup" });
    expect(parseAppRoute("/lab-tracker/app/setup")).toEqual({ kind: "setup" });
  });

  it("parses ongoing-project onboarding with contextual query state", () => {
    expect(
      parseAppRoute(`/app/projects/${PROJECT_ID}/onboarding?from=setup`)
    ).toEqual({ kind: "member-onboarding", projectId: PROJECT_ID });
    expect(
      parseAppRoute(`/lab-tracker/app/projects/${PROJECT_ID}/onboarding`)
    ).toEqual({ kind: "member-onboarding", projectId: PROJECT_ID });
  });

  it("parses app routes under a GitHub Pages project prefix", () => {
    expect(parseAppRoute("/lab-tracker/app/graph")).toEqual({ kind: "graph" });
    expect(parseAppRoute(`/lab-tracker/app/questions/${QUESTION_ID}`)).toEqual({
      kind: "question",
      questionId: QUESTION_ID,
    });
  });

  it("preserves the current base path when resolving app links", () => {
    expect(appBasePath("/lab-tracker/app")).toBe("/lab-tracker");
    expect(resolveAppPath("/app/graph", "/lab-tracker/app")).toBe(
      "/lab-tracker/app/graph"
    );
    expect(resolveAppPath("/app/graph", "/app")).toBe("/app/graph");
  });

  it("lets an absent contextual project reach its denied or not-found request after projects load", () => {
    expect(isContextualProjectReady({
      projectId: PROJECT_ID,
      projects: [],
      projectsLoaded: false,
      selectedProjectId: "",
    })).toBe(false);
    expect(isContextualProjectReady({
      projectId: PROJECT_ID,
      projects: [],
      projectsLoaded: true,
      selectedProjectId: "",
    })).toBe(true);
    expect(isContextualProjectReady({
      projectId: PROJECT_ID,
      projects: [{ project_id: PROJECT_ID }],
      projectsLoaded: true,
      selectedProjectId: "another-project",
    })).toBe(false);
  });
});

describe("AppLink", () => {
  afterEach(() => {
    window.history.replaceState({}, "", "/");
  });

  it("renders a prefixed href and navigates to that same prefixed path", () => {
    window.history.replaceState({}, "", "/lab-tracker/app");
    const navigate = vi.fn();
    render(
      <AppLink to="/app/graph" navigate={navigate}>
        Graph
      </AppLink>
    );
    const link = screen.getByRole("link", { name: "Graph" });
    // href (used by copy-link / open-in-new-tab / native navigation) is prefixed
    expect(link.getAttribute("href")).toBe("/lab-tracker/app/graph");
    fireEvent.click(link);
    // ...and intercepted navigation resolves to the identical URL.
    expect(navigate).toHaveBeenCalledWith("/lab-tracker/app/graph");
  });

  it("renders an unprefixed href at a root deployment", () => {
    window.history.replaceState({}, "", "/app");
    const navigate = vi.fn();
    render(
      <AppLink to="/app/graph" navigate={navigate}>
        Graph
      </AppLink>
    );
    expect(screen.getByRole("link", { name: "Graph" }).getAttribute("href")).toBe(
      "/app/graph"
    );
  });

  it("leaves external links untouched and does not intercept them", () => {
    window.history.replaceState({}, "", "/lab-tracker/app");
    const navigate = vi.fn();
    render(
      <AppLink to="https://example.com/docs" navigate={navigate}>
        Docs
      </AppLink>
    );
    const link = screen.getByRole("link", { name: "Docs" });
    expect(link.getAttribute("href")).toBe("https://example.com/docs");
    fireEvent.click(link, { preventDefault: () => {} });
    expect(navigate).not.toHaveBeenCalled();
  });
});
