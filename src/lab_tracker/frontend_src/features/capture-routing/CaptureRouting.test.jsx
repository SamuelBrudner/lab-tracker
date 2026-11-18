import * as React from "react";
import { fireEvent, render, screen } from "@testing-library/react";
import { expect, it } from "vitest";

import { apiResponse, installFetchMock } from "../../test/utils.js";
import { UnassignedCaptures } from "./CaptureRouting.jsx";

it("shows overlapping sessions and waits for an explicit operator choice", async () => {
  const path = "/review-operator/note/capture";
  const requests = installFetchMock([
    { match: "/projects/project/unassigned-machine-captures", response: apiResponse([{
      note_id: "capture",
      title: "Rig booking",
      source: "ics",
      captured_at: "2026-09-22T12:00:00Z",
      route: { status: "needs_session", session_ids: [] },
      clock_sessions: ["11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222"],
    }]) },
    { match: "/projects/project/members?limit=200", response: apiResponse([
      { user_id: "observer", username: "Observer", role: "viewer" },
      { user_id: "scientist", username: "Scientist", role: "contributor" },
    ]) },
    { match: path, method: "PUT", response: apiResponse({ user_id: "scientist" }) },
  ]);
  render(<UnassignedCaptures token="session" projectId="project" navigate={() => {}} />);
  fireEvent.click(screen.getByRole("button", { name: "Unassigned machine captures" }));
  expect(await screen.findByText("2 sessions overlap the capture time.")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Save operator" })).toBeDisabled();
  expect(screen.queryByRole("option", { name: "Observer" })).toBeNull();
  fireEvent.change(screen.getByLabelText("Review operator"), { target: { value: "scientist" } });
  fireEvent.click(screen.getByRole("button", { name: "Save operator" }));
  expect(await screen.findByRole("status")).toHaveTextContent("Capture assigned for review.");
  expect(requests).toHaveBeenCalledWith(path, expect.objectContaining({
    method: "PUT", body: JSON.stringify({ user_id: "scientist" }),
  }));
});
