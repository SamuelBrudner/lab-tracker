import * as React from "react";

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AUTH_REJECTED_EVENT } from "../../shared/api.js";
import { apiResponse, errorResponse, installFetchMock } from "../../test/utils.js";
import { visualization } from "../../test/fixtures.js";

import { VisualizationDetailCard } from "./VisualizationDetailCard.jsx";

function imageVisualization() {
  return {
    ...visualization({ vizId: "viz-asset" }),
    asset: {
      checksum: "abc123",
      content_type: "image/png",
      filename: "figure.png",
      size_bytes: 42,
    },
    asset_download_path: "/visualizations/viz-asset/asset",
  };
}

describe("VisualizationDetailCard", () => {
  const originalCreateObjectURL = URL.createObjectURL;
  const originalRevokeObjectURL = URL.revokeObjectURL;

  beforeEach(() => {
    URL.createObjectURL = vi.fn(() => "blob:viz-preview");
    URL.revokeObjectURL = vi.fn();
  });

  afterEach(() => {
    URL.createObjectURL = originalCreateObjectURL;
    URL.revokeObjectURL = originalRevokeObjectURL;
  });

  it("reports a failed asset download instead of leaving an unhandled rejection", async () => {
    installFetchMock([
      { match: "/visualizations/viz-asset", response: apiResponse(imageVisualization()) },
      {
        match: "/visualizations/viz-asset/asset",
        response: [
          errorResponse("Preview bytes are missing.", 404),
          errorResponse("Visualization asset bytes are missing.", 404),
        ],
      },
    ]);

    render(<VisualizationDetailCard token="token-viz" vizId="viz-asset" navigate={vi.fn()} />);

    // The preview and the download request the same URL, so the queued
    // responses are handed out in call order. The preview fetch starts in an
    // effect that can still be pending when the button first renders; settle it
    // before clicking so the download is the second request.
    expect(
      await screen.findByText("Preview unavailable: Preview bytes are missing.")
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Download asset" }));

    expect(
      await screen.findByText("Visualization asset bytes are missing.")
    ).toBeInTheDocument();
  });

  it("loads the preview through the shared transport and reports its failure", async () => {
    const authRejected = vi.fn();
    window.addEventListener(AUTH_REJECTED_EVENT, authRejected);
    try {
      installFetchMock([
        { match: "/visualizations/viz-asset", response: apiResponse(imageVisualization()) },
        {
          match: "/visualizations/viz-asset/asset",
          response: errorResponse("Session expired.", 401),
        },
      ]);

      render(<VisualizationDetailCard token="token-viz" vizId="viz-asset" navigate={vi.fn()} />);

      expect(
        await screen.findByText("Preview unavailable: Session expired.")
      ).toBeInTheDocument();
      expect(authRejected).toHaveBeenCalledTimes(1);
    } finally {
      window.removeEventListener(AUTH_REJECTED_EVENT, authRejected);
    }
  });
});

describe("VisualizationDetailCard Back", () => {
  function renderWithDepth(depth) {
    window.history.replaceState(
      depth ? { labTracker: { depth } } : null,
      "",
      "/app/visualizations/viz-1"
    );
    installFetchMock([{ match: "/visualizations/viz-1", response: apiResponse(visualization()) }]);
    const navigate = vi.fn();
    render(<VisualizationDetailCard token="token-viz" vizId="viz-1" navigate={navigate} />);
    return navigate;
  }

  it("Back uses in-app history with /app fallback", async () => {
    const back = vi.spyOn(window.history, "back").mockImplementation(() => {});

    const direct = renderWithDepth(0);
    fireEvent.click(await screen.findByRole("button", { name: "Back" }));
    expect(direct).toHaveBeenCalledWith("/app");
    expect(back).not.toHaveBeenCalled();
    cleanup();

    const fromApp = renderWithDepth(1);
    fireEvent.click(await screen.findByRole("button", { name: "Back" }));
    expect(back).toHaveBeenCalledTimes(1);
    expect(fromApp).not.toHaveBeenCalled();
  });
});
