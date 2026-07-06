import { describe, expect, it } from "vitest";

import {
  buildFlowGraph,
  computeQuestionLayout,
  defaultHiddenTypes,
  filterGraphByHiddenTypes,
} from "./project-graph.jsx";

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

  it("anchors a downstream node to its source's row, not the top of the column", () => {
    const graph = {
      nodes: [
        q("R", "root"),
        q("S", "sub"),
        { id: "D", entity_type: "dataset", label: "dataset" },
        { id: "A", entity_type: "analysis", label: "analysis" },
      ],
      edges: [
        edge("e1", "R", "S", "question_parent"),
        edge("e2", "S", "D", "dataset_question_primary"),
        edge("e3", "D", "A", "analysis_dataset"),
      ],
    };
    const byId = Object.fromEntries(
      buildFlowGraph(graph, "evidence").nodes.map((n) => [n.id, n]),
    );
    // The sub-question S sits below the root R; the dataset and analysis follow
    // S's row rather than floating at the top of their columns.
    expect(byId.S.position.y).toBeGreaterThan(byId.R.position.y);
    expect(byId.D.position.y).toBe(byId.S.position.y);
    expect(byId.A.position.y).toBe(byId.D.position.y);
    // ...and they sit in successive columns to the right.
    expect(byId.D.position.x).toBeGreaterThan(byId.S.position.x);
    expect(byId.A.position.x).toBeGreaterThan(byId.D.position.x);
  });

  it("places goals at the right edge and dashes candidate goal edges", () => {
    const graph = {
      nodes: [
        q("Q", "question"),
        { id: "V", entity_type: "visualization", label: "figure" },
        { id: "G", entity_type: "goal", label: "paper" },
      ],
      edges: [
        edge("e1", "Q", "V", "visualization_claim"),
        edge("e2", "V", "G", "goal_candidate_figure_candidate", "candidate figure"),
      ],
    };
    const result = buildFlowGraph(graph, "evidence");
    const byId = Object.fromEntries(result.nodes.map((node) => [node.id, node]));
    const goalEdge = result.edges.find((item) => item.id === "e2");

    expect(byId.G.position.x).toBeGreaterThan(byId.V.position.x);
    expect(byId.G.style.clipPath).toContain("polygon");
    expect(goalEdge.style.strokeDasharray).toBe("5 5");
  });

  it("truncates long display labels but keeps the full text in data", () => {
    const longLabel = "word ".repeat(60).trim(); // 299 chars
    const graph = {
      nodes: [{ id: "C", entity_type: "claim", label: longLabel }],
      edges: [],
    };
    const [node] = buildFlowGraph(graph, "evidence").nodes;
    expect(node.data.label.length).toBeLessThanOrEqual(110);
    expect(node.data.label.endsWith("…")).toBe(true);
    expect(node.data.fullLabel).toBe(longLabel);
  });

  it("does not split an astral-plane character at the truncation boundary", () => {
    // The emoji straddles the 108/109 UTF-16 boundary; a code-unit slice would
    // leave a lone high surrogate (rendered as the replacement character).
    const label = `${"a".repeat(108)}😀${"b".repeat(50)}`;
    const graph = { nodes: [{ id: "C", entity_type: "claim", label }], edges: [] };
    const [node] = buildFlowGraph(graph, "evidence").nodes;
    const beforeEllipsis = node.data.label.slice(0, -1);
    // No unpaired surrogate anywhere in the displayed label.
    expect(/[\uD800-\uDFFF]/.test(beforeEllipsis.replace(/[\uD800-\uDBFF][\uDC00-\uDFFF]/g, ""))).toBe(false);
    expect(node.data.label.endsWith("…")).toBe(true);
  });

  it("leaves short labels untouched", () => {
    const graph = {
      nodes: [{ id: "C", entity_type: "claim", label: "short claim" }],
      edges: [],
    };
    const [node] = buildFlowGraph(graph, "evidence").nodes;
    expect(node.data.label).toBe("short claim");
  });

  it("places exploration nodes between claims and visualizations", () => {
    const graph = {
      nodes: [
        q("Q", "question"),
        { id: "C", entity_type: "claim", label: "claim" },
        { id: "E", entity_type: "exploration_node", label: "dead end" },
        { id: "V", entity_type: "visualization", label: "figure" },
      ],
      edges: [
        edge("e1", "C", "Q", "claim_question_answers"),
        edge("e2", "C", "E", "exploration_target", "concerns"),
        edge("e3", "C", "V", "visualization_claim", "related claim"),
      ],
    };
    const byId = Object.fromEntries(
      buildFlowGraph(graph, "evidence").nodes.map((node) => [node.id, node]),
    );

    expect(byId.E.data.entityType).toBe("exploration_node");
    expect(byId.E.position.x).toBeGreaterThan(byId.C.position.x);
    expect(byId.V.position.x).toBeGreaterThan(byId.E.position.x);
  });
});

// A question with two downstream chains: Q -> D -> A and Q -> D2. Used to
// exercise on-demand edge labels and neighborhood dimming.
function evidenceChain() {
  return {
    nodes: [
      q("Q", "question"),
      { id: "D", entity_type: "dataset", label: "ds" },
      { id: "D2", entity_type: "dataset", label: "ds2" },
      { id: "A", entity_type: "analysis", label: "an" },
    ],
    edges: [
      edge("e1", "Q", "D", "dataset_question_primary", "primary question"),
      edge("e2", "Q", "D2", "dataset_question_primary", "primary question"),
      edge("e3", "D", "A", "analysis_dataset", "used by"),
    ],
  };
}

describe("buildFlowGraph edge labels and selection", () => {
  it("renders no edge labels by default", () => {
    const { edges } = buildFlowGraph(evidenceChain(), "evidence");
    expect(edges.every((item) => item.label === undefined)).toBe(true);
    // ...but keeps the prose available for on-demand display.
    expect(edges[0].data.label).toBe("primary question");
  });

  it("renders every edge label when showEdgeLabels is on", () => {
    const { edges } = buildFlowGraph(evidenceChain(), "evidence", {
      showEdgeLabels: true,
    });
    expect(edges.map((item) => item.label)).toEqual([
      "primary question",
      "primary question",
      "used by",
    ]);
  });

  it("labels only the hovered edge", () => {
    const { edges } = buildFlowGraph(evidenceChain(), "evidence", {
      hoveredEdgeId: "e3",
    });
    const byId = Object.fromEntries(edges.map((item) => [item.id, item]));
    expect(byId.e3.label).toBe("used by");
    expect(byId.e1.label).toBeUndefined();
    expect(byId.e2.label).toBeUndefined();
  });

  it("dims everything outside the selected node's neighborhood and labels its edges", () => {
    const { nodes, edges } = buildFlowGraph(evidenceChain(), "evidence", {
      selectedNodeId: "D",
    });
    const nodesById = Object.fromEntries(nodes.map((item) => [item.id, item]));
    const edgesById = Object.fromEntries(edges.map((item) => [item.id, item]));
    // D's 1-hop neighborhood is Q and A; D2 dims.
    expect(nodesById.D.style.opacity).toBeUndefined();
    expect(nodesById.Q.style.opacity).toBeUndefined();
    expect(nodesById.A.style.opacity).toBeUndefined();
    expect(nodesById.D2.style.opacity).toBe(0.15);
    expect(nodesById.D.style.borderWidth).toBe(2);
    expect(nodesById.D2.style.borderWidth).toBe(1);
    // Incident edges keep opacity and show labels; the rest dim, unlabeled.
    expect(edgesById.e1.label).toBe("primary question");
    expect(edgesById.e3.label).toBe("used by");
    expect(edgesById.e1.style.opacity).toBeUndefined();
    expect(edgesById.e2.label).toBeUndefined();
    expect(edgesById.e2.style.opacity).toBe(0.15);
  });

  it("ignores a selected id that is not in the rendered graph", () => {
    const { nodes } = buildFlowGraph(evidenceChain(), "evidence", {
      selectedNodeId: "note:gone",
    });
    expect(nodes.every((item) => item.style.opacity === undefined)).toBe(true);
  });
});

describe("type filtering", () => {
  it("hides notes and sessions by default in the full view only", () => {
    expect([...defaultHiddenTypes("full")].sort()).toEqual(["note", "session"]);
    expect(defaultHiddenTypes("evidence").size).toBe(0);
    expect(defaultHiddenTypes("questions").size).toBe(0);
  });

  it("drops hidden types and their incident edges, and re-stacks survivors from the top", () => {
    const graph = {
      nodes: [
        { id: "N", entity_type: "note", label: "note" },
        { id: "D", entity_type: "dataset", label: "ds" },
        { id: "D2", entity_type: "dataset", label: "ds2" },
      ],
      edges: [edge("e1", "N", "D", "note_target", "about")],
    };
    const filtered = filterGraphByHiddenTypes(graph, new Set(["note"]));
    expect(filtered.nodes.map((node) => node.id)).toEqual(["D", "D2"]);
    expect(filtered.edges).toHaveLength(0);
    // Layout of the filtered graph stacks the surviving column from y=0.
    const { nodes } = buildFlowGraph(filtered, "evidence");
    expect(nodes[0].position.y).toBe(0);
    expect(nodes[1].position.y).toBe(118);
  });

  it("returns the graph unchanged when nothing is hidden", () => {
    const graph = { nodes: [q("Q", "question")], edges: [] };
    expect(filterGraphByHiddenTypes(graph, new Set())).toBe(graph);
    expect(filterGraphByHiddenTypes(null, new Set(["note"]))).toBe(null);
  });
});
