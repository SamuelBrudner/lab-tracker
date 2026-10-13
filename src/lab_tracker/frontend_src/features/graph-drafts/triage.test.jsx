import * as React from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { GraphDraftDetailCard } from "../graph-drafts.jsx";
import { apiResponse, installFetchMock } from "../../test/utils.js";

function fixture() {
  const operation = (id, extra = {}) => ({ operation_id: id, sequence: 0, entity_type: "question", op: "create", semantic_type: "suggest_new_question", payload: { text: "Which comparison matters next?" }, status: "proposed", source_refs: [{ quote: "Compare the two groups", source_note_ids: ["note-1"] }], ...extra });
  return {
    change_set_id: "draft-1", project_id: "project-1", status: "ready", draft_mode: "graph_batch",
    operations: [operation("question"), operation("evidence", { entity_type: "note", payload: { raw_content: "Observation from the comparison" } }), operation("link", { op: "update", entity_type: "note", semantic_type: "link_note_to_question", payload: { targets: [{ entity_type: "question", entity_id: "existing" }] } })],
    context_packet: {}, summary: "Account of the day", clarification_requests: [], uncertain_fields: [],
    triage: {
      summary: { judgment: 1, confirmations: 1, later: 0, deferred: 0 },
      groups: [
        { group_id: "question", kind: "operations", lane: "judgment", question: "Does this accurately reflect the work?", reason: "Confirm the meaning", operation_ids: ["question", "evidence"], pending_operation_ids: ["question", "evidence"], deferred_operation_ids: [], resolved: false },
        { group_id: "link", kind: "operations", lane: "confirmations", question: "Are these capture links correct?", reason: "Adds capture links", operation_ids: ["link"], pending_operation_ids: ["link"], deferred_operation_ids: [], resolved: false },
      ],
    },
  };
}

function mount(draft, routes = [], props = {}) {
  installFetchMock([{ match: "/graph-drafts/draft-1", response: apiResponse(draft) }, ...routes]);
  return render(<GraphDraftDetailCard token="token" changeSetId="draft-1" navigate={vi.fn()} canWrite canManageGraph user={{ role: "admin", user_id: "user-1" }} setBusy={vi.fn()} setFlash={vi.fn()} backPath="/app/batches" {...props} />);
}

describe("default review triage", () => {
  it("presents a meaningful decision and atomically keeps only its related edits", async () => {
    const draft = fixture();
    let body;
    const accepted = structuredClone(draft);
    accepted.operations.slice(0, 2).forEach((op) => { op.status = "accepted"; });
    accepted.triage.groups[0].resolved = true;
    accepted.triage.summary.judgment = 0;
    mount(draft, [{ match: "/graph-drafts/draft-1/triage-decision", method: "POST", response: (request) => { body = JSON.parse(request.init.body); return apiResponse(accepted); } }]);
    expect(await screen.findByRole("region", { name: "Review triage" })).toBeInTheDocument();
    expect(screen.getByText("2 decisions need your input")).toBeInTheDocument();
    expect(screen.getByText("Account of the day")).toBeVisible();
    expect(screen.queryByRole("button", { name: "Accept all" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Correct" }));
    fireEvent.change(screen.getByLabelText("Text"), { target: { value: "The corrected comparison" } });
    fireEvent.click(screen.getByRole("button", { name: "Keep this decision" }));
    await waitFor(() => expect(body?.operation_ids).toEqual(["question", "evidence"]));
    expect(body.edits.question.payload.text).toBe("The corrected comparison");
    expect(await screen.findByText("Are these capture links correct?")).toBeInTheDocument();
  });

  it("restores deferred decisions and pauses without submitting or committing", async () => {
    const draft = fixture();
    draft.triage.groups[0] = { ...draft.triage.groups[0], lane: "later", deferred: true, pending_operation_ids: [], deferred_operation_ids: ["question", "evidence"] };
    draft.triage.summary = { judgment: 0, confirmations: 1, later: 1, deferred: 1 };
    const navigate = vi.fn();
    mount(draft, [], { navigate });
    await screen.findByRole("region", { name: "Review triage" });
    fireEvent.click(screen.getByRole("button", { name: "Can wait (1)" }));
    expect(screen.getByRole("button", { name: "Return to review" })).toBeEnabled();
    fireEvent.click(screen.getByRole("button", { name: "Save progress and return to queue" }));
    expect(navigate).toHaveBeenCalledWith("/app/batches");
    expect(globalThis.fetch.mock.calls.every(([, options]) => options?.method !== "POST")).toBe(true);
  });

  it("saves an answer without accepting graph edits", async () => {
    const draft = fixture();
    draft.triage.groups = [{ kind: "clarification", group_id: "clarification", question: "Which session does this belong to?", operation_ids: [], lane: "judgment", resolved: false }];
    draft.triage.summary = { judgment: 1, confirmations: 0, later: 0 };
    let body;
    mount(draft, [{ match: "/graph-drafts/draft-1/triage-clarification", method: "POST", response: (request) => { body = JSON.parse(request.init.body); return apiResponse({ ...draft, triage: { summary: { judgment: 0, confirmations: 0, later: 0 }, groups: [] } }); } }]);
    await screen.findByLabelText("Your answer");
    expect(screen.getByRole("button", { name: "Save answer" })).toBeDisabled();
    fireEvent.change(screen.getByLabelText("Your answer"), { target: { value: "The afternoon session" } });
    fireEvent.click(screen.getByRole("button", { name: "Save answer" }));
    await waitFor(() => expect(body).toEqual({ question: "Which session does this belong to?", decision: "answered", answer: "The afternoon session" }));
    expect(await screen.findByText("No graph decisions need review")).toBeInTheDocument();
  });

  it("distinguishes owner commit from saved contributor decisions", async () => {
    const draft = { ...fixture(), status: "submitted" };
    mount(draft);
    expect(await screen.findByText("Your decisions are saved. Awaiting owner commit.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Keep this decision" })).toBeDisabled();
  });

  it("lets the owner close a submitted review with no kept graph changes", async () => {
    const draft = { ...fixture(), status: "submitted" };
    draft.operations.forEach((operation) => { operation.status = "rejected"; });
    draft.triage.groups.forEach((group) => { group.resolved = true; });
    draft.triage.summary = { judgment: 0, confirmations: 0, later: 0 };
    let body;
    mount(draft, [{ match: "/graph-drafts/draft-1/review", method: "POST", response: (request) => {
      body = JSON.parse(request.init.body);
      return apiResponse({ ...draft, status: "rejected" });
    } }]);
    fireEvent.click(await screen.findByRole("button", { name: "Close review without graph changes" }));
    await waitFor(() => expect(body).toEqual({ status: "rejected", note: null }));
    expect(await screen.findByText("Review finished without kept graph changes")).toBeInTheDocument();
  });
});
