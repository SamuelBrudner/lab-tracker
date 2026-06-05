import { describe, expect, it } from "vitest";

import { buildFlowGraph, computeQuestionLayout } from "./project-graph.jsx";

const q = (id, label) => ({ id, entity_type: "question", label });
const edge = (id, source, target, relationship, label) => ({
  id,
  source,
  target,
  relationship,
  label: label ?? relationship,
});

// A motivating question M supersedes R and branches into A and B.
function supersedeTree(labels = {}) {
  return {
    nodes: [q("R", "root"), q("M", "motivating"), q("A", "childA"), q("B", "childB")],
    edges: [
      edge("e1", "R", "M", "question_superseded_by", labels.sup ?? "superseded by"),
      edge("e2", "M", "R", "question_supersedes", labels.rev ?? "supersedes"),
      edge("e3", "M", "A", "question_parent", labels.parent ?? "parent"),
      edge("e4", "M", "B", "question_parent", labels.parent ?? "parent"),
    ],
  };
}

describe("computeQuestionLayout", () => {
  it("derives a tree depth from parent/superseded-by edges and ignores the reverse", () => {
    const { depth } = computeQuestionLayout(supersedeTree().nodes, supersedeTree().edges);
    expect(depth.get("R")).toBe(0);
    expect(depth.get("M")).toBe(1); // would be 0 if the reverse "supersedes" leaked in
    expect(depth.get("A")).toBe(2);
    expect(depth.get("B")).toBe(2);
  });

  it("keys off relationship, not the display label (rewording must not flatten)", () => {
    const reworded = supersedeTree({ sup: "Superseded By!", parent: "is a parent of" });
    const { depth } = computeQuestionLayout(reworded.nodes, reworded.edges);
    expect(depth.get("M")).toBe(1);
    expect(depth.get("A")).toBe(2);
  });

  it("assigns finite, distinct rows to sibling leaves", () => {
    const { row } = computeQuestionLayout(supersedeTree().nodes, supersedeTree().edges);
    expect(Number.isFinite(row.get("A"))).toBe(true);
    expect(Number.isFinite(row.get("B"))).toBe(true);
    expect(row.get("A")).not.toBe(row.get("B"));
  });

  it("uses longest-path depth so every downward edge of a DAG points downward", () => {
    const nodes = [q("A", "a"), q("B", "b"), q("C", "c")];
    const edges = [
      edge("e1", "A", "B", "question_parent"),
      edge("e2", "B", "C", "question_parent"),
      edge("e3", "A", "C", "question_parent"), // C has parents at depth 0 and 1
    ];
    const { depth } = computeQuestionLayout(nodes, edges);
    expect(depth.get("A")).toBe(0);
    expect(depth.get("B")).toBe(1);
    expect(depth.get("C")).toBe(2); // longest path, not 1
  });

  it("does not loop or throw on a cycle", () => {
    const nodes = [q("A", "a"), q("B", "b")];
    const edges = [
      edge("e1", "A", "B", "question_parent"),
      edge("e2", "B", "A", "question_parent"),
    ];
    const { depth } = computeQuestionLayout(nodes, edges);
    expect(Number.isFinite(depth.get("A"))).toBe(true);
    expect(Number.isFinite(depth.get("B"))).toBe(true);
  });
});

describe("buildFlowGraph edge dedupe", () => {
  it("collapses a mutual supersede pair to the downward edge regardless of order", () => {
    // reverse "supersedes" listed FIRST to prove order-independence
    const graph = {
      nodes: [q("R", "root"), q("M", "motivating")],
      edges: [
        edge("e2", "M", "R", "question_supersedes", "supersedes"),
        edge("e1", "R", "M", "question_superseded_by", "superseded by"),
      ],
    };
    const { edges } = buildFlowGraph(graph, "questions");
    expect(edges).toHaveLength(1);
    expect(edges[0].source).toBe("R");
    expect(edges[0].target).toBe("M");
  });

  it("preserves non-question nodes' column positions", () => {
    const graph = {
      nodes: [
        { id: "d1", entity_type: "dataset", label: "ds" },
        { id: "d2", entity_type: "dataset", label: "ds2" },
      ],
      edges: [],
    };
    const { nodes } = buildFlowGraph(graph, "evidence");
    expect(nodes[0].position.y).toBe(0);
    expect(nodes[1].position.y).toBe(118);
    expect(nodes[0].position.x).toBe(nodes[1].position.x);
  });
});
