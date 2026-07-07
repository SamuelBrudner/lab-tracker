import "@testing-library/jest-dom/vitest";
import { cleanup, configure } from "@testing-library/react";
import { afterEach, beforeEach, vi } from "vitest";

// findBy*/waitFor default to a 1000ms timeout, which is ample locally but too
// tight on a loaded CI runner: an async chain like POST /questions -> refresh
// -> re-render can occasionally exceed it, intermittently failing assertions
// such as findByText("Question staged."). Give async queries real headroom so
// CI load spikes don't cause spurious timeouts; genuinely missing UI still
// fails, just after a longer wait.
configure({ asyncUtilTimeout: 5000 });

vi.mock("@xyflow/react", async () => {
  const React = await import("react");
  const ReactFlow = ({ children, edges = [], nodes = [], onNodeClick }) =>
    React.createElement(
      "div",
      { "data-testid": "react-flow" },
      nodes.map((node) =>
        React.createElement(
          "button",
          {
            key: node.id,
            type: "button",
            onClick: (event) => onNodeClick?.(event, node),
          },
          node.data?.label || node.id
        )
      ),
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
});
