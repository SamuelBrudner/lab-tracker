import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { apiFetch, apiListRequest, apiRequest, apiTextRequest } from "./api.js";

describe("static demo API", () => {
  beforeEach(() => {
    globalThis.__LAB_TRACKER_STATIC_DEMO__ = true;
  });

  afterEach(() => {
    delete globalThis.__LAB_TRACKER_STATIC_DEMO__;
  });

  it("serves a read-only authenticated demo session", async () => {
    const auth = await apiFetch("/auth/me");

    expect(auth.meta.auth_enabled).toBe(true);
    expect(auth.meta.demo).toBe(true);
    expect(auth.data.username).toBe("demo.viewer");
    expect(auth.data.role).toBe("viewer");
  });

  it("lists seeded project records with FastAPI-style pagination metadata", async () => {
    const { data, meta } = await apiListRequest("/projects?limit=200");

    expect(meta.total).toBe(1);
    expect(data[0].name).toBe("Antennal-lobe gain control");
  });

  it("filters seeded records and returns graph payloads", async () => {
    const questions = await apiListRequest(
      "/questions?project_id=2a32290d-5977-4e1a-9639-23210d3d4f1e&status=active&limit=200"
    );
    const graph = await apiRequest(
      "/projects/2a32290d-5977-4e1a-9639-23210d3d4f1e/graph?view=questions"
    );
    const mermaid = await apiTextRequest(
      "/projects/2a32290d-5977-4e1a-9639-23210d3d4f1e/graph/mermaid?view=questions"
    );

    expect(questions.data).toHaveLength(4);
    expect(graph.nodes.every((node) => node.entity_type === "question")).toBe(true);
    expect(mermaid).toContain("graph LR");
  });

  it("labels the dataset node with its manifest name, not the commit hash", async () => {
    const graph = await apiRequest(
      "/projects/2a32290d-5977-4e1a-9639-23210d3d4f1e/graph?view=evidence"
    );
    const dataset = graph.nodes.find((node) => node.entity_type === "dataset");
    expect(dataset).toBeTruthy();
    expect(dataset.label).toBe("2025_12_10_Rig2_session001.nwb");
    // ...and the commit hash stays available on the node detail.
    expect(dataset.detail).toContain("f2eabaa");
  });

  it("rejects writes so the public demo is safe to click through", async () => {
    await expect(
      apiRequest("/projects", {
        body: { name: "Should not persist" },
        method: "POST",
      })
    ).rejects.toThrow("hosted demo is read-only");
  });
});
