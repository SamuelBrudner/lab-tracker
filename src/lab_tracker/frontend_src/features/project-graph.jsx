import * as React from "react";
import { Background, Controls, MiniMap, ReactFlow } from "@xyflow/react";
import "@xyflow/react/dist/style.css";

import { apiRequest, apiTextRequest, buildApiPath } from "../shared/api.js";

const GRAPH_VIEWS = [
  { id: "evidence", label: "Evidence" },
  { id: "questions", label: "Questions" },
  { id: "full", label: "Full Graph" },
];

const TYPE_LABELS = {
  analysis: "Analysis",
  claim: "Claim",
  dataset: "Dataset",
  exploration_node: "Exploration",
  goal: "Goal",
  note: "Note",
  question: "Question",
  session: "Session",
  visualization: "Visualization",
};

const TYPE_LAYER_BY_VIEW = {
  evidence: {
    question: 0,
    dataset: 1,
    analysis: 2,
    claim: 3,
    exploration_node: 4,
    visualization: 5,
    goal: 6,
  },
  questions: {
    question: 0,
  },
  full: {
    note: 0,
    question: 1,
    session: 2,
    dataset: 3,
    analysis: 4,
    claim: 5,
    exploration_node: 6,
    visualization: 7,
    goal: 8,
  },
};

const TYPE_STYLES = {
  analysis: { background: "#eef3f9", borderColor: "#8ba3c7" },
  claim: { background: "#f7f0f4", borderColor: "#c98da9" },
  dataset: { background: "#edf5ed", borderColor: "#85aa83" },
  exploration_node: { background: "#eef6f7", borderColor: "#579aa5" },
  goal: { background: "#fff4cf", borderColor: "#c99724" },
  note: { background: "#fbf3e7", borderColor: "#d3a96b" },
  question: { background: "#f6f2eb", borderColor: "#b8a77d" },
  session: { background: "#edf4f5", borderColor: "#75a8b0" },
  visualization: { background: "#f1eef8", borderColor: "#9a8bc7" },
};

const VIEW_AXIS_LABELS = {
  evidence: "Evidence flow",
  full: "Full graph flow",
  questions: "Question links",
};

const VIEW_AXIS_TYPES = {
  evidence: [
    "question",
    "dataset",
    "analysis",
    "claim",
    "exploration_node",
    "visualization",
    "goal",
  ],
  full: [
    "note",
    "question",
    "session",
    "dataset",
    "analysis",
    "claim",
    "exploration_node",
    "visualization",
    "goal",
  ],
  questions: ["question"],
};

// Column width per entity-type layer, and row height per stacked node.
const COL_WIDTH = 265;
const ROW_HEIGHT = 118;
// Display cap for on-canvas node text. Roughly three wrapped lines at the
// fixed 220px node width, so no node grows taller than ROW_HEIGHT and
// overlaps the row below. Full text lives in data.fullLabel and the
// selection detail panel.
const NODE_LABEL_DISPLAY_LIMIT = 110;
// Edges read as background structure: light stroke and small arrowheads by
// default, darker when hovered so the revealed label has a visible anchor.
const EDGE_STROKE = "#c6ccd2";
const EDGE_STROKE_ACTIVE = "#6b7280";
const DIMMED_OPACITY = 0.15;

function truncateNodeLabel(label) {
  const text = String(label ?? "");
  // Slice by code point, not UTF-16 unit, so an emoji or other astral-plane
  // character straddling the cut boundary is never split into a lone surrogate.
  const points = Array.from(text);
  if (points.length <= NODE_LABEL_DISPLAY_LIMIT) {
    return text;
  }
  return `${points.slice(0, NODE_LABEL_DISPLAY_LIMIT - 1).join("").trimEnd()}…`;
}

// Per-view starting visibility. The full view opens with the two
// highest-volume, lowest-signal types hidden; their legend chips keep
// showing counts so the information is one click away.
export function defaultHiddenTypes(view) {
  return view === "full" ? new Set(["note", "session"]) : new Set();
}

// Drop hidden entity types and any edge touching them. Runs BEFORE layout so
// the surviving columns re-stack compactly instead of leaving holes.
export function filterGraphByHiddenTypes(graph, hiddenTypes) {
  if (!graph || !hiddenTypes || hiddenTypes.size === 0) {
    return graph;
  }
  const nodes = (graph.nodes || []).filter(
    (node) => !hiddenTypes.has(node.entity_type),
  );
  const visibleIds = new Set(nodes.map((node) => node.id));
  const edges = (graph.edges || []).filter(
    (edge) => visibleIds.has(edge.source) && visibleIds.has(edge.target),
  );
  return { ...graph, nodes, edges };
}
// Questions form a hierarchy (parent / supersede), so they are laid out as a
// tree from their edges instead of a single insertion-ordered column. We key
// off the stable machine `relationship` field (NOT the human display `label`,
// which is prose and may be reworded/localized). These relationships point
// "downward" (parent -> child, predecessor -> successor); the reverse
// "question_supersedes" edge is intrinsically excluded, avoiding a 2-cycle.
const QUESTION_DOWN_RELATIONSHIPS = new Set([
  "question_parent",
  "question_superseded_by",
]);
const Q_TREE_COL = 250; // depth spacing in the all-questions tree view
const Q_TREE_ROW = 120; // sibling spacing in the all-questions tree view

function groupByType(nodes) {
  return nodes.reduce((groups, node) => {
    const items = groups[node.entity_type] || [];
    items.push(node);
    groups[node.entity_type] = items;
    return groups;
  }, {});
}

// Compute a tidy-tree position (depth + row) for every question node from the
// question hierarchy edges, plus a preorder index used to order questions when
// they share an entity-type column (full / evidence views). The question graph
// is a DAG (a question may have multiple parents), so depth is a longest-path
// pass (every downward edge then points to a strictly greater depth) and row
// centering is over the full child set. Exported for unit testing.
export function computeQuestionLayout(nodes, edges) {
  const order = (nodes || [])
    .filter((node) => node.entity_type === "question")
    .map((node) => node.id);
  const qids = new Set(order);
  const childrenOf = new Map(order.map((id) => [id, []]));
  const parentsOf = new Map(order.map((id) => [id, []]));
  (edges || []).forEach((edge) => {
    if (!qids.has(edge.source) || !qids.has(edge.target)) return;
    if (!QUESTION_DOWN_RELATIONSHIPS.has(edge.relationship)) return;
    if (edge.source === edge.target) return;
    const kids = childrenOf.get(edge.source);
    if (kids.includes(edge.target)) return;
    kids.push(edge.target);
    parentsOf.get(edge.target).push(edge.source);
  });

  // Longest-path depth (DAG-safe), memoized with a cycle guard.
  const depth = new Map();
  function depthOf(id, stack) {
    if (depth.has(id)) return depth.get(id);
    if (stack.has(id)) return 0; // break cycles
    stack.add(id);
    let d = 0;
    parentsOf.get(id).forEach((p) => {
      d = Math.max(d, depthOf(p, stack) + 1);
    });
    stack.delete(id);
    depth.set(id, d);
    return d;
  }
  order.forEach((id) => depthOf(id, new Set()));

  // Tidy rows + preorder index. Recurse only into not-yet-placed children to
  // stay acyclic, but center each node over the rows of ALL of its children.
  const row = new Map();
  const preIndex = new Map();
  const placed = new Set();
  let nextLeafRow = 0;
  let nextPre = 0;
  function place(id) {
    if (placed.has(id)) return;
    placed.add(id);
    preIndex.set(id, nextPre);
    nextPre += 1;
    childrenOf.get(id).forEach((c) => {
      if (!placed.has(c)) place(c);
    });
    const childRows = childrenOf
      .get(id)
      .map((c) => row.get(c))
      .filter((r) => r != null);
    row.set(
      id,
      childRows.length
        ? (Math.min(...childRows) + Math.max(...childRows)) / 2
        : nextLeafRow++,
    );
  }
  // Roots (no parents) first, in stable order, then any leftovers (cycles).
  order.filter((id) => parentsOf.get(id).length === 0).forEach(place);
  order.forEach(place);
  return { qids, depth, row, preIndex };
}

function nodeLayer(node, layerByType) {
  return layerByType[node.entity_type] ?? 0;
}

// Compute an (x, y) for every node. Questions use the tree layout (questions
// view) or an ordered entity-type column (evidence / full). Every other node is
// anchored to the row of its upstream source(s) — the dataset to its primary
// question, the analysis to its dataset, and so on — so each chain reads
// horizontally and a node never appears to hang off a question it is not linked
// to. Unanchored nodes fall back to stacking from the top of their column.
function computeNodePositions(nodes, edges, view, layerByType, qLayout) {
  const positions = new Map();
  const nodesById = new Map((nodes || []).map((node) => [node.id, node]));
  const isTreeQuestion = (node) =>
    node.entity_type === "question" && qLayout.qids.has(node.id);

  (nodes || []).filter(isTreeQuestion).forEach((node) => {
    if (view === "questions") {
      positions.set(node.id, {
        x: (qLayout.depth.get(node.id) ?? 0) * Q_TREE_COL,
        y: (qLayout.row.get(node.id) ?? 0) * Q_TREE_ROW,
      });
    } else {
      positions.set(node.id, {
        x: nodeLayer(node, layerByType) * COL_WIDTH,
        y: (qLayout.preIndex.get(node.id) ?? 0) * ROW_HEIGHT,
      });
    }
  });

  const incoming = new Map();
  (edges || []).forEach((edge) => {
    if (!incoming.has(edge.target)) incoming.set(edge.target, []);
    incoming.get(edge.target).push(edge.source);
  });

  const nonQuestion = (nodes || [])
    .filter((node) => !isTreeQuestion(node))
    .map((node, index) => ({ node, index }))
    .sort(
      (a, b) =>
        nodeLayer(a.node, layerByType) - nodeLayer(b.node, layerByType) ||
        a.index - b.index,
    )
    .map((entry) => entry.node);

  const fallbackByLayer = new Map();
  const usedYByLayer = new Map();
  nonQuestion.forEach((node) => {
    const layer = nodeLayer(node, layerByType);
    const sourceYs = (incoming.get(node.id) || [])
      .map((sourceId) => ({
        pos: positions.get(sourceId),
        src: nodesById.get(sourceId),
      }))
      .filter(
        (entry) =>
          entry.pos && entry.src && nodeLayer(entry.src, layerByType) < layer,
      )
      .map((entry) => entry.pos.y);
    let y;
    if (sourceYs.length) {
      y = sourceYs.reduce((sum, value) => sum + value, 0) / sourceYs.length;
    } else {
      const count = fallbackByLayer.get(layer) || 0;
      fallbackByLayer.set(layer, count + 1);
      y = count * ROW_HEIGHT;
    }
    const used = usedYByLayer.get(layer) || [];
    while (used.some((other) => Math.abs(other - y) < ROW_HEIGHT * 0.75)) {
      y += ROW_HEIGHT;
    }
    used.push(y);
    usedYByLayer.set(layer, used);
    positions.set(node.id, { x: layer * COL_WIDTH, y });
  });

  return positions;
}

function graphNodeToFlowNode(node, positions, selection) {
  const style = TYPE_STYLES[node.entity_type] || {};
  const goalShape =
    node.entity_type === "goal"
      ? { clipPath: "polygon(8% 0, 92% 0, 100% 50%, 92% 100%, 8% 100%, 0 50%)" }
      : {};
  const isSelected = selection.selectedNodeId === node.id;
  const isDimmed = selection.active && !selection.neighborIds.has(node.id);
  return {
    id: node.id,
    data: {
      detail: node.detail,
      entityType: node.entity_type,
      fullLabel: node.label,
      label: truncateNodeLabel(node.label),
      route: node.route,
      status: node.status,
    },
    position: positions.get(node.id) || { x: 0, y: 0 },
    style: {
      ...style,
      borderWidth: isSelected ? 2 : 1,
      color: "#1f2933",
      ...goalShape,
      maxWidth: 220,
      width: 220,
      ...(isSelected
        ? { boxShadow: `0 0 0 2px ${style.borderColor || "#6b7280"}` }
        : {}),
      ...(isDimmed ? { opacity: DIMMED_OPACITY } : {}),
    },
  };
}

function graphEdgeToFlowEdge(edge, selection, { hoveredEdgeId, showEdgeLabels }) {
  const isCandidate = String(edge.relationship || "").endsWith("_candidate");
  const isHovered = edge.id === hoveredEdgeId;
  const isIncident = selection.active && selection.incidentEdgeIds.has(edge.id);
  const isDimmed = selection.active && !isIncident;
  // Prose labels are on-demand: hover an edge, select a node to label its
  // incident edges, or opt back into always-on via the toolbar checkbox.
  const showLabel = showEdgeLabels || isHovered || isIncident;
  // Graph edges are directed (parent -> child, dataset -> analysis, ...), so
  // render an arrowhead at the target end.
  const stroke = isHovered || isIncident ? EDGE_STROKE_ACTIVE : EDGE_STROKE;
  return {
    id: edge.id,
    ...(showLabel ? { label: edge.label } : {}),
    data: { label: edge.label, relationship: edge.relationship },
    source: edge.source,
    target: edge.target,
    type: "smoothstep",
    markerEnd: { type: "arrowclosed", width: 14, height: 14, color: stroke },
    style: {
      stroke,
      strokeDasharray: isCandidate ? "5 5" : undefined,
      ...(isDimmed ? { opacity: DIMMED_OPACITY } : {}),
    },
  };
}

export function buildFlowGraph(graph, view, options = {}) {
  const {
    hoveredEdgeId = null,
    selectedNodeId = null,
    showEdgeLabels = false,
  } = options;
  const layerByType = TYPE_LAYER_BY_VIEW[view] || TYPE_LAYER_BY_VIEW.evidence;
  const qLayout = computeQuestionLayout(graph?.nodes || [], graph?.edges || []);
  const positions = computeNodePositions(
    graph?.nodes || [],
    graph?.edges || [],
    view,
    layerByType,
    qLayout,
  );
  // Collapse mutual A<->B pairs (e.g. question_superseded_by + its reverse
  // question_supersedes) to one directed edge so arrowheads don't render two
  // opposing arrows. Process "downward" relationships first so the surviving
  // edge points in the meaningful direction regardless of backend edge order.
  const orderedEdges = [...(graph?.edges || [])].sort((a, b) => {
    const aDown = QUESTION_DOWN_RELATIONSHIPS.has(a.relationship) ? 0 : 1;
    const bDown = QUESTION_DOWN_RELATIONSHIPS.has(b.relationship) ? 0 : 1;
    return aDown - bDown;
  });
  const keptPairs = new Set();
  const dedupedEdges = orderedEdges.filter((edge) => {
    if (keptPairs.has(`${edge.target}->${edge.source}`)) return false;
    keptPairs.add(`${edge.source}->${edge.target}`);
    return true;
  });
  // Focus-on-selection: the selected node's 1-hop neighborhood keeps full
  // opacity (and its incident edges show their labels); everything else dims.
  // Selection only activates when the id is actually in the rendered graph,
  // so a stale id (e.g. its type was just hidden) never dims the whole canvas.
  const active =
    Boolean(selectedNodeId) &&
    (graph?.nodes || []).some((node) => node.id === selectedNodeId);
  const neighborIds = new Set(active ? [selectedNodeId] : []);
  const incidentEdgeIds = new Set();
  if (active) {
    dedupedEdges.forEach((edge) => {
      if (edge.source === selectedNodeId || edge.target === selectedNodeId) {
        incidentEdgeIds.add(edge.id);
        neighborIds.add(edge.source);
        neighborIds.add(edge.target);
      }
    });
  }
  const selection = { active, incidentEdgeIds, neighborIds, selectedNodeId };
  return {
    edges: dedupedEdges.map((edge) =>
      graphEdgeToFlowEdge(edge, selection, { hoveredEdgeId, showEdgeLabels }),
    ),
    nodes: (graph?.nodes || []).map((node) =>
      graphNodeToFlowNode(node, positions, selection),
    ),
  };
}

function legendTypesForView(view, nodeGroups) {
  const viewTypes = VIEW_AXIS_TYPES[view] || VIEW_AXIS_TYPES.evidence;
  const extraTypes = Object.keys(nodeGroups)
    .filter((entityType) => !viewTypes.includes(entityType))
    .sort();
  return [...viewTypes, ...extraTypes];
}

function downloadTextFile({ contents, filename }) {
  const blob = new Blob([contents], { type: "text/vnd.mermaid;charset=utf-8" });
  const objectUrl = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = objectUrl;
  anchor.download = filename;
  anchor.style.display = "none";
  document.body.appendChild(anchor);
  try {
    anchor.click();
  } finally {
    anchor.remove();
    window.setTimeout(() => URL.revokeObjectURL(objectUrl), 0);
  }
}

function ProjectGraphExplorer({
  navigate,
  onSelectedProjectChange,
  projects = [],
  selectedProjectId = "",
  setFlash,
  token,
}) {
  const [view, setView] = React.useState(() => {
    const requested = new URLSearchParams(window.location.search).get("view");
    return GRAPH_VIEWS.some((option) => option.id === requested) ? requested : "evidence";
  });
  const [hiddenTypes, setHiddenTypes] = React.useState(() => defaultHiddenTypes(view));
  const [selectedNodeId, setSelectedNodeId] = React.useState(null);
  const [hoveredEdgeId, setHoveredEdgeId] = React.useState(null);
  const [showEdgeLabels, setShowEdgeLabels] = React.useState(false);
  const selectView = React.useCallback((nextView) => {
    setView(nextView);
    setHiddenTypes(defaultHiddenTypes(nextView));
    const url = new URL(window.location.href);
    url.searchParams.set("view", nextView);
    window.history.replaceState(window.history.state, "", url);
  }, []);
  const toggleType = React.useCallback((entityType) => {
    setHiddenTypes((current) => {
      const next = new Set(current);
      if (next.has(entityType)) {
        next.delete(entityType);
      } else {
        next.add(entityType);
      }
      return next;
    });
  }, []);
  const [graph, setGraph] = React.useState(null);
  const [loading, setLoading] = React.useState(false);
  const [error, setError] = React.useState("");
  const [exporting, setExporting] = React.useState(false);

  React.useEffect(() => {
    let canceled = false;
    setGraph(null);
    setError("");
    setSelectedNodeId(null);
    setHoveredEdgeId(null);
    if (!selectedProjectId) {
      return () => {
        canceled = true;
      };
    }
    setLoading(true);
    apiRequest(buildApiPath(`/projects/${selectedProjectId}/graph`, { view }), { token })
      .then((nextGraph) => {
        if (!canceled) {
          setGraph(nextGraph);
        }
      })
      .catch((err) => {
        if (!canceled) {
          setError(err.message || "Failed to load project graph.");
        }
      })
      .finally(() => {
        if (!canceled) {
          setLoading(false);
        }
      });
    return () => {
      canceled = true;
    };
  }, [selectedProjectId, token, view]);

  const filteredGraph = React.useMemo(
    () => filterGraphByHiddenTypes(graph, hiddenTypes),
    [graph, hiddenTypes],
  );
  const selectedNode = React.useMemo(
    () =>
      (filteredGraph?.nodes || []).find((node) => node.id === selectedNodeId) ||
      null,
    [filteredGraph, selectedNodeId],
  );
  const flowGraph = React.useMemo(
    () =>
      buildFlowGraph(filteredGraph, view, {
        hoveredEdgeId,
        selectedNodeId,
        showEdgeLabels,
      }),
    [filteredGraph, hoveredEdgeId, selectedNodeId, showEdgeLabels, view],
  );
  // Counts come from the unfiltered graph so a hidden type's chip still
  // reports how much it is hiding.
  const nodeGroups = React.useMemo(() => groupByType(graph?.nodes || []), [graph]);
  const legendTypes = React.useMemo(
    () => legendTypesForView(view, nodeGroups),
    [nodeGroups, view]
  );
  const selectedProject = projects.find((project) => project.project_id === selectedProjectId);

  async function loadMermaid() {
    if (!selectedProjectId) {
      return "";
    }
    return apiTextRequest(
      buildApiPath(`/projects/${selectedProjectId}/graph/mermaid`, { view }),
      { token }
    );
  }

  async function handleCopyMermaid() {
    if (!selectedProjectId || exporting) {
      return;
    }
    setExporting(true);
    try {
      const mermaid = await loadMermaid();
      await navigator.clipboard.writeText(mermaid);
      setFlash("Mermaid graph copied.");
    } catch (err) {
      setFlash("", err.message || "Failed to copy Mermaid graph.");
    } finally {
      setExporting(false);
    }
  }

  async function handleDownloadMermaid() {
    if (!selectedProjectId || exporting) {
      return;
    }
    setExporting(true);
    try {
      const mermaid = await loadMermaid();
      downloadTextFile({
        contents: mermaid,
        filename: `lab-tracker-${selectedProjectId}-${view}.mmd`,
      });
      setFlash("Mermaid graph downloaded.");
    } catch (err) {
      setFlash("", err.message || "Failed to download Mermaid graph.");
    } finally {
      setExporting(false);
    }
  }

  return (
    <article className="card span-12 project-graph-card">
      <div className="item-head">
        <div>
          <h2>Project Graph</h2>
          {selectedProject ? <p className="subtle">{selectedProject.name}</p> : null}
        </div>
        <div className="inline">
          <button type="button" className="btn-secondary" onClick={() => navigate("/app")}>
            Back
          </button>
          <button
            type="button"
            className="btn-secondary"
            disabled={!selectedProjectId || exporting}
            onClick={handleCopyMermaid}
          >
            Copy Mermaid
          </button>
          <button
            type="button"
            className="btn-secondary"
            disabled={!selectedProjectId || exporting}
            onClick={handleDownloadMermaid}
          >
            Download Mermaid
          </button>
        </div>
      </div>

      <label className="project-graph-project-picker">
        Active project
        <select value={selectedProjectId} onChange={onSelectedProjectChange}>
          <option value="">Select a project</option>
          {projects.map((project) => (
            <option key={project.project_id} value={project.project_id}>
              {project.name}
            </option>
          ))}
        </select>
      </label>

      <div className="tabs" role="tablist" aria-label="Project graph views">
        {GRAPH_VIEWS.map((graphView) => (
          <button
            type="button"
            role="tab"
            aria-selected={view === graphView.id}
            className={view === graphView.id ? "tab active" : "tab"}
            key={graphView.id}
            onClick={() => selectView(graphView.id)}
          >
            {graphView.label}
          </button>
        ))}
      </div>

      {!selectedProjectId ? (
        <p className="subtle">Select a project to load its graph.</p>
      ) : null}
      {loading ? <p className="subtle">Loading project graph...</p> : null}
      {error ? <p className="flash error">{error}</p> : null}

      {selectedProjectId && !loading && !error && graph?.nodes?.length === 0 ? (
        <p className="subtle">No graph records for this view.</p>
      ) : null}

      {selectedProjectId && !loading && !error && graph?.nodes?.length > 0 ? (
        <>
          <div className="project-graph-guide">
            <div
              className="project-graph-axis"
              role="group"
              aria-label="Graph layout order and node type filters"
            >
              <span className="project-graph-axis-label">{VIEW_AXIS_LABELS[view]}</span>
              {legendTypes.map((entityType, index) => {
                const style = TYPE_STYLES[entityType] || {};
                const typeLabel = TYPE_LABELS[entityType] || entityType;
                const hidden = hiddenTypes.has(entityType);
                return (
                  <React.Fragment key={entityType}>
                    <button
                      type="button"
                      className={
                        hidden
                          ? "project-graph-legend-item is-hidden"
                          : "project-graph-legend-item"
                      }
                      aria-pressed={!hidden}
                      title={
                        hidden
                          ? `Show ${typeLabel} nodes`
                          : `Hide ${typeLabel} nodes`
                      }
                      onClick={() => toggleType(entityType)}
                    >
                      <span
                        className="project-graph-legend-swatch"
                        style={{
                          background: style.background,
                          borderColor: style.borderColor,
                        }}
                      />
                      {typeLabel}: {nodeGroups[entityType]?.length || 0}
                    </button>
                    {index < legendTypes.length - 1 ? (
                      <span className="project-graph-axis-arrow" aria-hidden="true">
                        →
                      </span>
                    ) : null}
                  </React.Fragment>
                );
              })}
              <span className="project-graph-legend-item project-graph-legend-key">
                <span
                  className="project-graph-legend-swatch project-graph-legend-swatch-dashed"
                  aria-hidden="true"
                />
                dashed = candidate
              </span>
              <label className="project-graph-legend-item project-graph-edge-label-toggle">
                <input
                  type="checkbox"
                  checked={showEdgeLabels}
                  onChange={(event) => setShowEdgeLabels(event.target.checked)}
                />
                Edge labels
              </label>
            </div>
          </div>
          <div className="project-graph-body">
            <div className="project-graph-canvas">
              <ReactFlow
                nodes={flowGraph.nodes}
                edges={flowGraph.edges}
                fitView
                nodesDraggable={false}
                nodesConnectable={false}
                elementsSelectable
                onNodeClick={(_event, node) => setSelectedNodeId(node.id)}
                onPaneClick={() => setSelectedNodeId(null)}
                onEdgeMouseEnter={(_event, edge) => setHoveredEdgeId(edge.id)}
                onEdgeMouseLeave={() => setHoveredEdgeId(null)}
              >
                <MiniMap
                  nodeColor={(node) =>
                    TYPE_STYLES[node.data?.entityType]?.background || "#e5e7eb"
                  }
                  nodeStrokeColor={(node) =>
                    TYPE_STYLES[node.data?.entityType]?.borderColor || "#9aa3ad"
                  }
                />
                <Controls />
                <Background />
              </ReactFlow>
            </div>
            {selectedNode ? (
              <aside
                className="project-graph-detail"
                aria-label="Selected node details"
              >
                <div className="project-graph-detail-head">
                  <span
                    className="project-graph-detail-type"
                    style={{
                      background: TYPE_STYLES[selectedNode.entity_type]?.background,
                      borderColor: TYPE_STYLES[selectedNode.entity_type]?.borderColor,
                    }}
                  >
                    {TYPE_LABELS[selectedNode.entity_type] || selectedNode.entity_type}
                  </span>
                  <button
                    type="button"
                    className="btn-secondary"
                    onClick={() => setSelectedNodeId(null)}
                  >
                    Close
                  </button>
                </div>
                <p className="project-graph-detail-label">{selectedNode.label}</p>
                {selectedNode.status ? (
                  <p className="subtle">Status: {selectedNode.status}</p>
                ) : null}
                {selectedNode.detail ? (
                  <p className="subtle">{selectedNode.detail}</p>
                ) : null}
                {selectedNode.route ? (
                  <button
                    type="button"
                    className="btn-secondary"
                    onClick={() => navigate(selectedNode.route)}
                  >
                    Open {TYPE_LABELS[selectedNode.entity_type] || selectedNode.entity_type}
                  </button>
                ) : null}
              </aside>
            ) : null}
          </div>
        </>
      ) : null}
    </article>
  );
}

export { ProjectGraphExplorer };
