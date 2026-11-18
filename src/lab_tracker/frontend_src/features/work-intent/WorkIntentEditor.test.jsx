import * as React from "react";
import { fireEvent, render, screen } from "@testing-library/react";
import { expect, it } from "vitest";

import { apiResponse, installFetchMock } from "../../test/utils.js";
import { WorkIntentEditor } from "./WorkIntentEditor.jsx";

it("saves work purpose independently of question links", async () => {
  const endpoint = "/work-intent/session/session-fixture";
  const saved = {
    kind: "calibration",
    source: "declared",
    recorded_at: "2026-09-22T12:00:00Z",
  };
  const requests = installFetchMock([
    { match: endpoint, response: apiResponse({ intent: null }) },
    { match: "/questions?project_id=project-fixture&limit=100", response: apiResponse([]) },
    { match: endpoint, method: "PUT", response: apiResponse(saved) },
  ]);
  render(<WorkIntentEditor token="session-token" scope="session" workId="session-fixture" projectId="project-fixture" canWrite />);
  fireEvent.click(screen.getByRole("button", { name: "Work purpose" }));
  fireEvent.change(await screen.findByLabelText("What was this work for?"), { target: { value: "calibration" } });
  fireEvent.click(screen.getByRole("button", { name: "Save work purpose" }));
  expect(await screen.findByRole("status")).toHaveTextContent("Calibration · Declared");
  expect(requests).toHaveBeenCalledWith(endpoint, expect.objectContaining({
    method: "PUT", body: JSON.stringify({ kind: "calibration" }),
  }));
});
