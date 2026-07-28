import { describe, expect, it } from "vitest";

import { appBasePath, parseAppRoute, resolveAppPath } from "./routing.jsx";

const QUESTION_ID = "fb3454e0-6319-40bb-864c-9de91d0b04f1";
const EXPERIMENT_ID = "cfde0ad8-e88a-498f-9c1c-9f12b4149c14";

describe("app routing", () => {
  it("parses the agent-access page route", () => {
    expect(parseAppRoute("/app/agents")).toEqual({ kind: "agents" });
    expect(parseAppRoute("/lab-tracker/app/agents")).toEqual({ kind: "agents" });
  });

  it("parses app routes under a GitHub Pages project prefix", () => {
    expect(parseAppRoute("/lab-tracker/app/graph")).toEqual({ kind: "graph" });
    expect(parseAppRoute(`/lab-tracker/app/questions/${QUESTION_ID}`)).toEqual({
      kind: "question",
      questionId: QUESTION_ID,
    });
  });

  it("parses Experiment detail routes", () => {
    expect(parseAppRoute(`/app/experiments/${EXPERIMENT_ID}`)).toEqual({
      kind: "experiment",
      experimentId: EXPERIMENT_ID,
    });
  });

  it("preserves the current base path when resolving app links", () => {
    expect(appBasePath("/lab-tracker/app")).toBe("/lab-tracker");
    expect(resolveAppPath("/app/graph", "/lab-tracker/app")).toBe(
      "/lab-tracker/app/graph"
    );
    expect(resolveAppPath("/app/graph", "/app")).toBe("/app/graph");
  });
});
