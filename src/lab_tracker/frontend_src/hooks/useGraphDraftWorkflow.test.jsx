import * as React from "react";

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { apiResponse, installFetchMock } from "../test/utils.js";
import { useGraphDraftWorkflow } from "./useGraphDraftWorkflow.js";

const CHANGE_SET_ID = "22222222-2222-4222-8222-222222222222";
const OPERATION_ID = "33333333-3333-4333-8333-333333333333";
const NOTE_ID = "11111111-1111-4111-8111-111111111111";

function draft() {
  return {
    change_set_id: CHANGE_SET_ID,
    operations: [
      {
        change_set_id: CHANGE_SET_ID,
        entity_type: "question",
        op: "create",
        operation_id: OPERATION_ID,
        payload: { text: "Does sleep change courtship behavior?" },
        status: "proposed",
      },
    ],
    project_id: "project-1",
    source_note_id: NOTE_ID,
    source_note_ids: [NOTE_ID],
    status: "ready",
  };
}

function Harness({ setFlash }) {
  const workflow = useGraphDraftWorkflow({
    canWrite: true,
    canManageGraph: true,
    changeSetId: CHANGE_SET_ID,
    setBusy: vi.fn(),
    setFlash,
    token: "token-1",
    user: { role: "admin", user_id: "user-1", username: "sam" },
  });
  const operation = workflow.changeSet?.operations?.[0];
  return (
    <div>
      <span data-testid="loaded">{operation ? "yes" : "no"}</span>
      <button
        type="button"
        disabled={!operation}
        onClick={() => workflow.rejectOperation(operation, "not_relevant")}
      >
        Reject with reason
      </button>
      <button type="button" disabled={!operation} onClick={() => workflow.deferOperation(operation)}>
        Defer
      </button>
      <button type="button" onClick={() => workflow.archiveSourceNote(NOTE_ID, "superseded")}>
        Set aside
      </button>
      <button
        type="button"
        onClick={async () => {
          const link = await workflow.decideProvenanceLink("link-1", "accepted");
          setFlash(`decided:${link?.status || "none"}`);
        }}
      >
        Accept link
      </button>
    </div>
  );
}

describe("useGraphDraftWorkflow", () => {
  it("rejects with a reason and defers through a deferred stamp", async () => {
    const patchBodies = [];
    installFetchMock([
      { match: `/graph-drafts/${CHANGE_SET_ID}`, response: apiResponse(draft()) },
      {
        match: `/graph-drafts/${CHANGE_SET_ID}/operations/${OPERATION_ID}`,
        method: "PATCH",
        response: (request) => {
          patchBodies.push(JSON.parse(request.init.body));
          return apiResponse(draft());
        },
      },
    ]);
    const setFlash = vi.fn();
    render(<Harness setFlash={setFlash} />);
    await waitFor(() => expect(screen.getByTestId("loaded")).toHaveTextContent("yes"));

    fireEvent.click(screen.getByRole("button", { name: "Reject with reason" }));
    await waitFor(() => expect(patchBodies).toHaveLength(1));
    expect(patchBodies[0]).toEqual({
      payload: { text: "Does sleep change courtship behavior?" },
      reject_reason: "not_relevant",
      review_note: null,
      status: "rejected",
    });
    expect(setFlash).toHaveBeenLastCalledWith("Rejected: Does sleep change courtship behavior?");

    fireEvent.click(screen.getByRole("button", { name: "Defer" }));
    await waitFor(() => expect(patchBodies).toHaveLength(2));
    expect(patchBodies[1]).toEqual({ deferred: true });
    expect(setFlash).toHaveBeenLastCalledWith("Deferred: Does sleep change courtship behavior?");
  });

  it("archives a source note with the chosen reason and reloads the draft", async () => {
    const archiveBodies = [];
    let draftLoads = 0;
    installFetchMock([
      {
        match: `/graph-drafts/${CHANGE_SET_ID}`,
        response: () => {
          draftLoads += 1;
          return apiResponse(draft());
        },
      },
      {
        match: `/notes/${NOTE_ID}/archive`,
        method: "POST",
        response: (request) => {
          archiveBodies.push(JSON.parse(request.init.body));
          return apiResponse({ note_id: NOTE_ID, status: "archived" });
        },
      },
    ]);
    const setFlash = vi.fn();
    render(<Harness setFlash={setFlash} />);
    await waitFor(() => expect(screen.getByTestId("loaded")).toHaveTextContent("yes"));

    fireEvent.click(screen.getByRole("button", { name: "Set aside" }));
    await waitFor(() => expect(archiveBodies).toEqual([{ reason: "superseded" }]));
    await waitFor(() => expect(setFlash).toHaveBeenLastCalledWith("Capture set aside (superseded)."));
    expect(draftLoads).toBe(2);
  });

  it("patches a provenance link status and hands back the updated link", async () => {
    const linkBodies = [];
    installFetchMock([
      { match: `/graph-drafts/${CHANGE_SET_ID}`, response: apiResponse(draft()) },
      {
        match: "/provenance-links/link-1",
        method: "PATCH",
        response: (request) => {
          linkBodies.push(JSON.parse(request.init.body));
          return apiResponse({ link_id: "link-1", status: "accepted" });
        },
      },
    ]);
    const setFlash = vi.fn();
    render(<Harness setFlash={setFlash} />);
    await waitFor(() => expect(screen.getByTestId("loaded")).toHaveTextContent("yes"));

    fireEvent.click(screen.getByRole("button", { name: "Accept link" }));
    await waitFor(() => expect(linkBodies).toEqual([{ status: "accepted" }]));
    await waitFor(() => expect(setFlash).toHaveBeenLastCalledWith("decided:accepted"));
  });
});
