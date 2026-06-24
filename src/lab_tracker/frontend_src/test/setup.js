import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach, beforeEach, vi } from "vitest";

vi.mock("@xyflow/react", async () => {
  const React = await import("react");
  const ReactFlow = ({ children, edges = [], nodes = [], nodeTypes = {}, onNodeClick }) =>
    React.createElement(
      "div",
      { "data-testid": "react-flow" },
      nodes.map((node) => {
        const NodeComponent = nodeTypes[node.type];
        return React.createElement(
          "button",
          {
            "aria-label": node.data?.label || node.id,
            key: node.id,
            type: "button",
            onClick: (event) => onNodeClick?.(event, node),
          },
          NodeComponent
            ? React.createElement(NodeComponent, { data: node.data })
            : node.data?.label || node.id
        );
      }),
      edges.map((edge) =>
        React.createElement("span", { key: edge.id, "data-testid": "react-flow-edge" }, edge.label)
      ),
      children
    );
  const Placeholder = ({ label }) =>
    React.createElement("span", { "data-testid": `react-flow-${label}` });
  return {
    Background: () => React.createElement(Placeholder, { label: "background" }),
    Controls: () => React.createElement(Placeholder, { label: "controls" }),
    MiniMap: () => React.createElement(Placeholder, { label: "minimap" }),
    ReactFlow,
  };
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

beforeEach(() => {
  localStorage.clear();
  window.history.replaceState({}, "", "/app");
  if (typeof URL.createObjectURL !== "function") {
    Object.defineProperty(URL, "createObjectURL", {
      configurable: true,
      value: vi.fn(() => "blob:mock-object-url"),
    });
  }
  if (typeof URL.revokeObjectURL !== "function") {
    Object.defineProperty(URL, "revokeObjectURL", {
      configurable: true,
      value: vi.fn(),
    });
  }
});
