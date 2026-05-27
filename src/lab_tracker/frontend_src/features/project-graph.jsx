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
    visualization: 4,
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
    visualization: 6,
  },
};

const TYPE_STYLES = {
  analysis: { background: "#eef3f9", borderColor: "#8ba3c7" },
  claim: { background: "#f7f0f4", borderColor: "#c98da9" },
  dataset: { background: "#edf5ed", borderColor: "#85aa83" },
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
  evidence: ["question", "dataset", "analysis", "claim", "visualization"],
  full: ["note", "question", "session", "dataset", "analysis", "claim", "visualization"],
  questions: ["question"],
};

function groupByType(nodes) {
  return nodes.reduce((groups, node) => {
    const items = groups[node.entity_type] || [];
    items.push(node);
    groups[node.entity_type] = items;
    return groups;
  }, {});
}

function graphNodeToFlowNode(node, indexByType, layerByType) {
  const typeIndex = indexByType[node.entity_type] || 0;
  indexByType[node.entity_type] = typeIndex + 1;
  const layer = layerByType[node.entity_type] ?? 0;
  const style = TYPE_STYLES[node.entity_type] || {};
  return {
    id: node.id,
    data: {
      detail: node.detail,
      entityType: node.entity_type,
      label: node.label,
      route: node.route,
      status: node.status,
    },
    position: {
      x: layer * 265,
      y: typeIndex * 118,
    },
    style: {
      ...style,
      borderWidth: 1,
      color: "#1f2933",
      maxWidth: 220,
      width: 220,
    },
  };
}

function graphEdgeToFlowEdge(edge) {
  return {
    id: edge.id,
    label: edge.label,
    source: edge.source,
    target: edge.target,
    type: "smoothstep",
  };
}

function buildFlowGraph(graph, view) {
  const indexByType = {};
  const layerByType = TYPE_LAYER_BY_VIEW[view] || TYPE_LAYER_BY_VIEW.evidence;
  return {
    edges: (graph?.edges || []).map(graphEdgeToFlowEdge),
    nodes: (graph?.nodes || []).map((node) =>
      graphNodeToFlowNode(node, indexByType, layerByType)
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
  const [view, setView] = React.useState("evidence");
  const [graph, setGraph] = React.useState(null);
  const [loading, setLoading] = React.useState(false);
  const [error, setError] = React.useState("");
  const [exporting, setExporting] = React.useState(false);

  React.useEffect(() => {
    let canceled = false;
    setGraph(null);
    setError("");
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

  const flowGraph = React.useMemo(() => buildFlowGraph(graph, view), [graph, view]);
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
            onClick={() => setView(graphView.id)}
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
            <div className="project-graph-axis" aria-label="Graph layout axis">
              <span className="project-graph-axis-label">{VIEW_AXIS_LABELS[view]}</span>
              <div className="project-graph-axis-steps">
                {legendTypes.map((entityType, index) => (
                  <React.Fragment key={entityType}>
                    <span className="project-graph-axis-step">
                      {TYPE_LABELS[entityType] || entityType}
                    </span>
                    {index < legendTypes.length - 1 ? (
                      <span className="project-graph-axis-arrow" aria-hidden="true">
                        →
                      </span>
                    ) : null}
                  </React.Fragment>
                ))}
              </div>
            </div>
            <div className="project-graph-legend" aria-label="Node color legend">
              {legendTypes.map((entityType) => {
                const style = TYPE_STYLES[entityType] || {};
                return (
                  <span className="project-graph-legend-item" key={entityType}>
                    <span
                      className="project-graph-legend-swatch"
                      style={{
                        background: style.background,
                        borderColor: style.borderColor,
                      }}
                    />
                    {TYPE_LABELS[entityType] || entityType}: {nodeGroups[entityType]?.length || 0}
                  </span>
                );
              })}
            </div>
          </div>
          <div className="project-graph-canvas">
            <ReactFlow
              nodes={flowGraph.nodes}
              edges={flowGraph.edges}
              fitView
              nodesDraggable={false}
              nodesConnectable={false}
              elementsSelectable={false}
              onNodeClick={(_event, node) => {
                if (node.data?.route) {
                  navigate(node.data.route);
                }
              }}
            >
              <MiniMap />
              <Controls />
              <Background />
            </ReactFlow>
          </div>
        </>
      ) : null}
    </article>
  );
}

export { ProjectGraphExplorer };
