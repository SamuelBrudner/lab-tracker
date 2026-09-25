import * as React from "react";

import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { GraphDraftDetailCard, spokenReviewScript } from "./graph-drafts.jsx";
import { REJECT_REASONS } from "./graph-drafts/review-reasons.js";
import {
  apiResponse,
  binaryResponse,
  errorResponse,
  installFetchMock,
} from "../test/utils.js";

function draftFixture(overrides = {}) {
  return {
    change_set_id: "22222222-2222-4222-8222-222222222222",
    clarification_requests: ["Confirm the control cohort"],
    context_packet: {},
    created_at: "2026-07-15T20:00:00Z",
    draft_mode: "graph_batch",
    model: "gpt-5.4-mini",
    operations: [
      {
        change_set_id: "22222222-2222-4222-8222-222222222222",
        confidence: 0.82,
        entity_type: "question",
        operation_id: "33333333-3333-4333-8333-333333333333",
        op: "create",
        payload: { text: "Does sleep change courtship behavior?" },
        rationale: "The capture states this as the next comparison.",
        semantic_type: "suggest_new_question",
        source_refs: [],
        status: "proposed",
      },
    ],
    project_id: "project-1",
    provider: "openai",
    source_note_id: "11111111-1111-4111-8111-111111111111",
    status: "ready",
    summary: "One new question was drafted from today's captures.",
    uncertain_fields: ["Exact sleep-deprivation window"],
    updated_at: "2026-07-15T20:00:00Z",
    ...overrides,
  };
}

function onboardingAccessFixture(capabilities = {}) {
  return {
    alignment: null,
    brief_markdown: "",
    capabilities: {
      can_align: true,
      can_capture: true,
      can_commit: false,
      can_create_checkpoint: true,
      can_read: true,
      ...capabilities,
    },
    checkpoint: null,
    first_capture: null,
    guided_fields: null,
    map_items: [],
    member_complete: false,
    owner_commit_pending: false,
    project_id: "project-1",
    role: "contributor",
    state: "not_started",
  };
}

function renderDraft(draft, extraProps = {}) {
  installFetchMock([
    { match: `/graph-drafts/${draft.change_set_id}`, response: apiResponse(draft) },
    ...(extraProps.routes || []),
  ]);
  const props = { ...extraProps };
  delete props.routes;
  return render(
    <GraphDraftDetailCard
      token="token-1"
      changeSetId={draft.change_set_id}
      navigate={vi.fn()}
      canWrite={true}
      canManageGraph={true}
      user={{ role: "admin", user_id: "user-1", username: "sam" }}
      setBusy={vi.fn()}
      setFlash={vi.fn()}
      {...props}
    />
  );
}

function installSpeechSynthesis() {
  class FakeSpeechSynthesisUtterance {
    constructor(text) {
      this.text = text;
    }
  }
  const speechSynthesis = {
    cancel: vi.fn(),
    pause: vi.fn(),
    resume: vi.fn(),
    speak: vi.fn(),
  };
  vi.stubGlobal("SpeechSynthesisUtterance", FakeSpeechSynthesisUtterance);
  Object.defineProperty(window, "speechSynthesis", {
    configurable: true,
    value: speechSynthesis,
  });
  return speechSynthesis;
}

afterEach(() => {
  vi.unstubAllGlobals();
  delete window.speechSynthesis;
  delete navigator.mediaDevices;
  delete URL.createObjectURL;
  delete URL.revokeObjectURL;
});

describe("spokenReviewScript", () => {
  it("turns a review into a concise summary, proposal, rationale, and question script", () => {
    const script = spokenReviewScript(draftFixture());

    expect(script).toContain("Review summary. One new question was drafted");
    expect(script).toContain("There is 1 proposal.");
    expect(script).toContain("Proposal 1. Proposed new question.");
    expect(script).toContain("Does sleep change courtship behavior?");
    expect(script).toContain("82 percent confidence.");
    expect(script).toContain(
      "Questions for you. Confirm the control cohort. Exact sleep-deprivation window."
    );
  });
});

describe("GraphDraftDetailCard narrative review", () => {
  it("uses loaded onboarding purpose to forbid bulk review and require submitted status for owner commit", async () => {
    const ready = draftFixture({
      purpose: "member_checkpoint_alignment",
      status: "ready",
      operations: [
        {
          ...draftFixture().operations[0],
          acceptance_mode: "human_selected",
          status: "accepted",
        },
      ],
    });
    const readyView = renderDraft(ready);

    expect(await screen.findByText("1 of 1 kept")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Accept all" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Commit accepted changes" })).toBeDisabled();
    readyView.unmount();

    const submitted = { ...ready, status: "submitted" };
    renderDraft(submitted);
    await screen.findByText("1 of 1 kept");
    expect(screen.queryByRole("button", { name: "Accept all" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Commit accepted changes" })).toBeEnabled();
  });

  it("hides AI revision for Daily Review batch drafts, which the API cannot revise", async () => {
    installSpeechSynthesis();
    renderDraft(draftFixture({ draft_mode: "graph_batch", status: "ready" }));

    expect(await screen.findByText(/reviewed proposal by proposal/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Listen to review" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Revise with AI" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Dictate feedback" })).not.toBeInTheDocument();
    expect(screen.queryByPlaceholderText(/Tell the AI how to revise/)).not.toBeInTheDocument();
  });

  it("offers AI revision for note-scoped drafts", async () => {
    renderDraft(draftFixture({ draft_mode: "graph_context", status: "ready" }));

    expect(await screen.findByRole("button", { name: "Revise with AI" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Dictate feedback" })).toBeInTheDocument();
    expect(screen.queryByText(/reviewed proposal by proposal/)).not.toBeInTheDocument();
  });

  it("keeps another member's onboarding draft read-only on the generic review route", async () => {
    const ready = draftFixture({
      draft_mode: "graph_context",
      created_by: "author-2",
      created_by_user_id: "author-2",
      purpose: "member_checkpoint_alignment",
      review_assignee: "author-2",
      review_assignee_user_id: "author-2",
      status: "changes_requested",
    });
    renderDraft(ready, {
      canManageGraph: false,
      user: { role: "viewer", user_id: "member-1", username: "member" },
      routes: [
        {
          match: "/projects/project-1/members?limit=200",
          response: apiResponse([{ role: "contributor", user_id: "member-1" }]),
        },
        {
          match: "/projects/project-1/member-onboarding",
          response: apiResponse(onboardingAccessFixture()),
        },
      ],
    });

    expect(await screen.findByLabelText("Edit JSON payload")).toBeDisabled();
    expect(screen.getByRole("button", { name: "Submit for review" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Revise with AI" })).toBeDisabled();
  });

  it("says why review actions are disabled when the membership lookup fails", async () => {
    renderDraft(draftFixture({ status: "submitted" }), {
      canManageGraph: false,
      user: { role: "viewer", user_id: "owner-1", username: "owner" },
      routes: [
        {
          match: "/projects/project-1/members?limit=200",
          response: errorResponse("Members service unavailable.", 503),
        },
      ],
    });

    expect(
      await screen.findByText(
        "Could not confirm your access to this project: Members service unavailable. Review actions stay disabled until it loads."
      )
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Accept all" })).toBeDisabled();
  });

  it("honors server-derived inherited owner capability for onboarding commit", async () => {
    const submitted = draftFixture({
      purpose: "member_checkpoint_alignment",
      status: "submitted",
      operations: [
        {
          ...draftFixture().operations[0],
          acceptance_mode: "human_selected",
          status: "accepted",
        },
      ],
    });
    renderDraft(submitted, {
      canManageGraph: false,
      user: { role: "viewer", user_id: "group-owner", username: "owner" },
      routes: [
        {
          match: "/projects/project-1/members?limit=200",
          response: apiResponse([]),
        },
        {
          match: "/projects/project-1/member-onboarding",
          response: apiResponse(onboardingAccessFixture({ can_commit: true })),
        },
      ],
    });

    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Commit accepted changes" })).toBeEnabled()
    );
  });

  it("keeps proposal cards as the default and offers a cited prose view of the same edits", async () => {
    const base = draftFixture();
    const secondOperation = {
      ...base.operations[0],
      entity_type: "note",
      operation_id: "44444444-4444-4444-8444-444444444444",
      payload: { raw_content: "Sleep-deprived flies courted less often." },
      rationale: "The capture describes this result directly.",
      semantic_type: "create_note",
    };
    renderDraft({ ...base, operations: [base.operations[0], secondOperation] });

    expect(await screen.findAllByLabelText("Edit JSON payload")).toHaveLength(2);
    expect(screen.getByRole("button", { name: "Proposals" })).toHaveAttribute(
      "aria-pressed",
      "true"
    );

    fireEvent.click(screen.getByRole("button", { name: "Narrative" }));

    const narrative = screen.getByRole("region", { name: "Narrative review" });
    expect(narrative).toHaveTextContent("The draft proposes to add a new question");
    expect(narrative).toHaveTextContent("It also proposes to add a research note");
    expect(narrative).toHaveTextContent("One new question was drafted from today's captures.");
    expect(
      screen.getByRole("button", { name: /Proposed edit 1: Proposed new question/ })
    ).toHaveTextContent("[1]");
    expect(
      screen.getByRole("button", { name: /Proposed edit 2: Proposed research note/ })
    ).toHaveTextContent("[2]");
    expect(screen.queryByLabelText("Edit JSON payload")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Proposals" }));
    expect(await screen.findAllByLabelText("Edit JSON payload")).toHaveLength(2);
  });

  it("opens citations by hover, focus, and click and routes every decision through the workflow", async () => {
    const draft = draftFixture({
      operations: [
        {
          ...draftFixture().operations[0],
          source_refs: [{ label: "voice memo", quote: "courtship dropped after deprivation" }],
        },
      ],
    });
    const patchBodies = [];
    let currentDraft = draft;
    renderDraft(draft, {
      routes: [
        {
          match: /\/graph-drafts\/[^/]+\/operations\//,
          method: "PATCH",
          response: (request) => {
            const body = JSON.parse(request.init.body);
            patchBodies.push(body);
            currentDraft = {
              ...currentDraft,
              operations: currentDraft.operations.map((operation) => ({
                ...operation,
                payload: body.payload,
                review_note: body.review_note,
                status: body.status,
              })),
            };
            return apiResponse(currentDraft);
          },
        },
      ],
    });

    fireEvent.click(await screen.findByRole("button", { name: "Narrative" }));
    const citation = screen.getByRole("button", {
      name: /Proposed edit 1: Proposed new question/,
    });

    fireEvent.focus(citation);
    expect(
      screen.getByRole("group", { name: "Proposed edit 1 details" })
    ).toHaveTextContent("courtship dropped after deprivation");
    fireEvent.keyDown(citation, { key: "Escape" });
    expect(
      screen.queryByRole("group", { name: "Proposed edit 1 details" })
    ).not.toBeInTheDocument();

    fireEvent.mouseEnter(citation.parentElement);
    expect(
      screen.getByRole("group", { name: "Proposed edit 1 details" })
    ).toBeInTheDocument();
    fireEvent.mouseLeave(citation.parentElement);
    expect(
      screen.queryByRole("group", { name: "Proposed edit 1 details" })
    ).not.toBeInTheDocument();

    fireEvent.click(citation);
    fireEvent.change(screen.getByLabelText("Note for proposed edit 1"), {
      target: { value: "Checked against the raw capture." },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save note" }));
    await waitFor(() => expect(patchBodies).toHaveLength(1));
    expect(patchBodies[0]).toMatchObject({
      review_note: "Checked against the raw capture.",
      status: "proposed",
    });

    // The action buttons are disabled while a save is pending. patchBodies is
    // appended by the fetch stub before React clears that pending state, so
    // clicking as soon as the array grows can land on a still-disabled button
    // and be dropped silently. Wait for the control to be re-enabled first.
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Accept edit" })).toBeEnabled()
    );
    fireEvent.click(screen.getByRole("button", { name: "Accept edit" }));
    await waitFor(() => expect(patchBodies).toHaveLength(2));
    expect(patchBodies[1]).toMatchObject({
      review_note: "Checked against the raw capture.",
      status: "accepted",
    });

    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Reject edit" })).toBeEnabled()
    );
    fireEvent.click(screen.getByRole("button", { name: "Reject edit" }));
    // A rejection names its reason: the citation offers the same chips as a card.
    const reasons = await screen.findByRole("group", { name: "Reason for rejecting edit 1" });
    fireEvent.click(within(reasons).getByRole("button", { name: /Other/ }));
    await waitFor(() => expect(patchBodies).toHaveLength(3));
    expect(patchBodies[2]).toMatchObject({
      reject_reason: "other",
      review_note: "Checked against the raw capture.",
      status: "rejected",
    });
  });
});

describe("GraphDraftDetailCard route identity", () => {
  const ID_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
  const ID_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";

  function baseProps(changeSetId) {
    return {
      token: "token-1",
      changeSetId,
      navigate: vi.fn(),
      canWrite: true,
      canManageGraph: true,
      user: { role: "admin", user_id: "user-1", username: "sam" },
      setBusy: vi.fn(),
      setFlash: vi.fn(),
    };
  }

  it("ignores a stale draft response after navigating to a different draft", async () => {
    const draftA = draftFixture({ change_set_id: ID_A, summary: "Draft A summary." });
    const draftB = draftFixture({ change_set_id: ID_B, summary: "Draft B summary." });

    let resolveA;
    const deferredA = new Promise((resolve) => {
      resolveA = resolve;
    });
    installFetchMock([
      { match: `/graph-drafts/${ID_A}`, response: () => deferredA },
      { match: `/graph-drafts/${ID_B}`, response: apiResponse(draftB) },
    ]);

    const { rerender } = render(<GraphDraftDetailCard {...baseProps(ID_A)} />);
    // Navigate to B while A's load is still in flight.
    rerender(<GraphDraftDetailCard {...baseProps(ID_B)} />);

    // B loads and renders; A was never shown.
    expect(await screen.findByText("Draft B summary.")).toBeInTheDocument();
    expect(screen.queryByText("Draft A summary.")).not.toBeInTheDocument();

    // A's response arrives late and must be ignored (last-started load wins).
    resolveA(apiResponse(draftA));
    await Promise.resolve();
    await Promise.resolve();

    expect(screen.queryByText("Draft A summary.")).not.toBeInTheDocument();
    expect(screen.getByText("Draft B summary.")).toBeInTheDocument();
  });

  it("targets a mutation at the route id, not the stale loaded draft", async () => {
    const draftA = draftFixture({ change_set_id: ID_A, summary: "Draft A summary." });
    const patchedUrls = [];
    const patchedBodies = [];
    installFetchMock([
      { match: `/graph-drafts/${ID_A}`, response: apiResponse(draftA) },
      {
        match: new RegExp(`/graph-drafts/${ID_A}/operations/`),
        method: "PATCH",
        response: (request) => {
          patchedUrls.push(request.url);
          patchedBodies.push(JSON.parse(request.init.body));
          return apiResponse({ ...draftA, status: "ready" });
        },
      },
    ]);

    render(<GraphDraftDetailCard {...baseProps(ID_A)} />);
    // Accept the single proposal; the mutation must hit the ROUTE id's URL.
    fireEvent.click(await screen.findByRole("button", { name: "Accept" }));
    await waitFor(() => expect(patchedUrls.length).toBe(1));
    expect(patchedUrls[0]).toContain(`/graph-drafts/${ID_A}/operations/`);
    expect(patchedBodies[0].review_note).toBeNull();
  });
});

describe("GraphDraftDetailCard accept all", () => {
  it("accepts all proposals via the atomic server endpoint and reports what remained", async () => {
    const draft = draftFixture();
    const partial = {
      ...draft,
      operations: [
        { ...draft.operations[0], status: "accepted" },
        {
          ...draft.operations[0],
          operation_id: "44444444-4444-4444-8444-444444444444",
          status: "proposed",
        },
      ],
    };
    const acceptAllUrls = [];
    const setFlash = vi.fn();
    renderDraft(draft, {
      setFlash,
      routes: [
        {
          match: `/graph-drafts/${draft.change_set_id}/accept-all`,
          method: "POST",
          response: (request) => {
            acceptAllUrls.push(request.url);
            return apiResponse(partial);
          },
        },
      ],
    });

    fireEvent.click(await screen.findByRole("button", { name: "Accept all" }));

    // One atomic request to the bulk endpoint — not a per-operation client loop.
    await waitFor(() => expect(acceptAllUrls).toHaveLength(1));
    expect(acceptAllUrls[0]).toContain("/accept-all");
    // Honest partial-failure report rather than blanket success.
    await waitFor(() =>
      expect(setFlash).toHaveBeenCalledWith(expect.stringContaining("could not be accepted"))
    );
  });

  it("guards against a duplicate accept-all while one is in flight", async () => {
    const draft = draftFixture();
    let resolveAccept;
    const deferred = new Promise((resolve) => {
      resolveAccept = resolve;
    });
    let calls = 0;
    renderDraft(draft, {
      routes: [
        {
          match: `/graph-drafts/${draft.change_set_id}/accept-all`,
          method: "POST",
          response: () => {
            calls += 1;
            return deferred;
          },
        },
      ],
    });

    const button = await screen.findByRole("button", { name: "Accept all" });
    fireEvent.click(button);
    fireEvent.click(button); // second click while the first is in flight
    await Promise.resolve();

    expect(calls).toBe(1);
    resolveAccept(apiResponse(draft));
    await waitFor(() => expect(button).not.toBeDisabled());
  });

  it("persists buffered payload and decision-note edits before bulk acceptance", async () => {
    const draft = draftFixture();
    const accepted = {
      ...draft,
      operations: [
        {
          ...draft.operations[0],
          acceptance_mode: "bulk_accepted",
          payload: { text: "Edited before accepting" },
          review_note: "Checked against the trace.",
          status: "accepted",
        },
      ],
    };
    const calls = [];
    const patchBodies = [];
    renderDraft(draft, {
      routes: [
        {
          match: `/graph-drafts/${draft.change_set_id}/operations/${draft.operations[0].operation_id}`,
          method: "PATCH",
          response: (request) => {
            calls.push("patch");
            patchBodies.push(JSON.parse(request.init.body));
            return apiResponse(draft);
          },
        },
        {
          match: `/graph-drafts/${draft.change_set_id}/accept-all`,
          method: "POST",
          response: () => {
            calls.push("accept-all");
            return apiResponse(accepted);
          },
        },
      ],
    });

    fireEvent.change(await screen.findByLabelText("Text"), {
      target: { value: "Edited before accepting" },
    });
    fireEvent.change(screen.getByLabelText("Decision note"), {
      target: { value: "Checked against the trace." },
    });
    fireEvent.click(screen.getByRole("button", { name: "Accept all" }));

    await screen.findByRole("button", { name: "Undo accept all" });
    expect(calls).toEqual(["patch", "accept-all"]);
    expect(patchBodies).toEqual([
      {
        payload: { text: "Edited before accepting" },
        review_note: "Checked against the trace.",
        status: "proposed",
      },
    ]);
  });

  it("blocks bulk acceptance when a buffered payload is invalid JSON", async () => {
    const draft = draftFixture();
    let acceptAllCalls = 0;
    const setFlash = vi.fn();
    renderDraft(draft, {
      setFlash,
      routes: [
        {
          match: `/graph-drafts/${draft.change_set_id}/accept-all`,
          method: "POST",
          response: () => {
            acceptAllCalls += 1;
            return apiResponse(draft);
          },
        },
      ],
    });

    fireEvent.change(await screen.findByLabelText("Edit JSON payload"), {
      target: { value: "{" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Accept all" }));

    await waitFor(() =>
      expect(setFlash).toHaveBeenCalledWith(
        "",
        "One proposal has invalid JSON. Fix or revert it before accepting all."
      )
    );
    expect(acceptAllCalls).toBe(0);
  });

  it("undoes only operations newly accepted by the latest bulk action", async () => {
    const draft = draftFixture();
    const handAcceptedId = "44444444-4444-4444-8444-444444444444";
    const handAccepted = {
      ...draft.operations[0],
      acceptance_mode: "human_selected",
      operation_id: handAcceptedId,
      status: "accepted",
    };
    const before = {
      ...draft,
      operations: [handAccepted, draft.operations[0]],
    };
    const afterAccept = {
      ...before,
      operations: [
        handAccepted,
        {
          ...draft.operations[0],
          acceptance_mode: "bulk_accepted",
          status: "accepted",
        },
      ],
    };
    const afterUndo = {
      ...afterAccept,
      operations: [
        handAccepted,
        {
          ...draft.operations[0],
          acceptance_mode: null,
          status: "proposed",
        },
      ],
    };
    const patchedUrls = [];
    const patchedBodies = [];
    renderDraft(before, {
      routes: [
        {
          match: `/graph-drafts/${draft.change_set_id}/accept-all`,
          method: "POST",
          response: apiResponse(afterAccept),
        },
        {
          match: `/graph-drafts/${draft.change_set_id}/operations/${draft.operations[0].operation_id}`,
          method: "PATCH",
          response: (request) => {
            patchedUrls.push(request.url);
            patchedBodies.push(JSON.parse(request.init.body));
            return apiResponse(afterUndo);
          },
        },
      ],
    });

    fireEvent.click(await screen.findByRole("button", { name: "Accept all" }));
    expect(await screen.findByText("1 proposal accepted as a batch.")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Undo accept all" }));

    await waitFor(() => expect(patchedUrls).toHaveLength(1));
    expect(patchedUrls[0]).toContain(draft.operations[0].operation_id);
    expect(patchedUrls[0]).not.toContain(handAcceptedId);
    expect(patchedBodies).toEqual([{ status: "proposed" }]);
    await waitFor(() =>
      expect(screen.queryByRole("button", { name: "Undo accept all" })).toBeNull()
    );
  });
});

describe("GraphDraftDetailCard figure evidence", () => {
  it("joins figures to proposals, scopes regions, reuses fetches, and shows provenance", async () => {
    const draft = draftFixture();
    const figureA = "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa";
    const figureB = "bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb";
    const operationA = {
      ...draft.operations[0],
      operation_id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
      payload: { text: "Proposal A" },
      source_refs: [
        {
          label: "panel A area",
          quote: "A rises",
          region: { height: 0.4, width: 0.3, x: 0.1, y: 0.2 },
          source_note_ids: [figureA],
          source_note_ids_resolution: "explicit",
        },
      ],
    };
    const operationB = {
      ...draft.operations[0],
      operation_id: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
      payload: { text: "Proposal B" },
      source_refs: [
        {
          label: "panel B area",
          quote: "B falls",
          region: { height: 0.2, width: 0.25, x: 0.55, y: 0.15 },
          source_note_ids: [figureB],
          source_note_ids_resolution: "explicit",
        },
      ],
    };
    const operationC = {
      ...draft.operations[0],
      operation_id: "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
      payload: { text: "Proposal C" },
      source_refs: [
        {
          label: "panel A replicate",
          source_note_ids: [figureA],
          source_note_ids_resolution: "explicit",
        },
      ],
    };
    const figureDraft = {
      ...draft,
      context_packet: {
        source_artifacts: [
          {
            checksum: "stored-checksum-a",
            content_type: "image/png",
            filename: "panel-a.png",
            metadata: {
              evidence_capture_kind: "figure",
              evidence_content_hash: "evidence-hash-a",
              evidence_source_uri: "file:///analysis/output/panel-a.png",
              run_code_file: "notebooks/figure_one.ipynb",
              run_code_line: 18,
              run_code_region_hash: "region-hash-a",
              run_code_symbol: "render_panel",
              run_git_commit: "abc123",
              run_git_dirty: true,
              run_repo_remote_url:
                "https://sam:ghp_secret@github.com/example/research.git?token=also-secret#fragment",
            },
            note_id: figureA,
            type: "image",
          },
          {
            checksum: "stored-checksum-b",
            content_type: "image/jpeg",
            filename: "panel-b.jpg",
            metadata: {
              evidence_capture_kind: "figure",
              evidence_source_uri:
                "https://sam:source_secret@analysis.example/panel-b.jpg?signature=hidden",
              run_git_dirty: false,
            },
            note_id: figureB,
            type: "image",
          },
        ],
      },
      operations: [operationA, operationB, operationC],
      source_content_type: "image/png",
      source_filename: "panel-a.png",
      source_note_id: figureA,
      source_note_ids: [figureA, figureB],
    };
    const rawCalls = { [figureA]: 0, [figureB]: 0 };
    let objectUrlSequence = 0;
    URL.createObjectURL = vi.fn(() => `blob:figure-${(objectUrlSequence += 1)}`);
    URL.revokeObjectURL = vi.fn();

    renderDraft(figureDraft, {
      routes: [
        {
          match: `/notes/${figureA}/raw`,
          response: () => {
            rawCalls[figureA] += 1;
            return binaryResponse({ body: "figure-a", contentType: "image/png" });
          },
        },
        {
          match: `/notes/${figureB}/raw`,
          response: () => {
            rawCalls[figureB] += 1;
            return binaryResponse({ body: "figure-b", contentType: "image/jpeg" });
          },
        },
        {
          match: `/graph-drafts/${draft.change_set_id}/operations/${operationA.operation_id}`,
          method: "PATCH",
          response: apiResponse({
            ...figureDraft,
            operations: [
              { ...operationA, status: "accepted" },
              operationB,
              operationC,
            ],
          }),
        },
      ],
    });

    const proposalAText = await screen.findByText("Proposal A", {
      selector: ".review-proposal-text",
    });
    const proposalA = proposalAText.closest(".review-proposal");
    const proposalB = screen
      .getByText("Proposal B", { selector: ".review-proposal-text" })
      .closest(".review-proposal");
    const proposalC = screen
      .getByText("Proposal C", { selector: ".review-proposal-text" })
      .closest(".review-proposal");

    expect(await within(proposalA).findByRole("img", { name: "Figure evidence: panel-a.png" }))
      .toBeInTheDocument();
    expect(within(proposalA).queryByRole("img", { name: "Figure evidence: panel-b.jpg" }))
      .not.toBeInTheDocument();
    expect(await within(proposalB).findByRole("img", { name: "Figure evidence: panel-b.jpg" }))
      .toBeInTheDocument();
    expect(within(proposalC).getByRole("img", { name: "Figure evidence: panel-a.png" }))
      .toBeInTheDocument();
    expect(within(proposalA).getByLabelText("Source region 1: panel A area"))
      .toBeInTheDocument();
    expect(within(proposalA).queryByLabelText("Source region 1: panel B area"))
      .not.toBeInTheDocument();

    expect(rawCalls).toEqual({ [figureA]: 1, [figureB]: 1 });
    expect(proposalA.querySelector(".source-artifact-code")).toHaveTextContent(
      "Generated by notebooks/figure_one.ipynb · render_panel · line 18"
    );
    expect(proposalB.querySelector(".source-artifact-code")).toHaveTextContent(
      "Source https://analysis.example/panel-b.jpg"
    );
    expect(proposalB).not.toHaveTextContent("source_secret");
    expect(proposalB).not.toHaveTextContent("signature=hidden");

    const detailsSummary = within(proposalA).getByText("Version & file details");
    const details = detailsSummary.closest("details");
    expect(details).not.toHaveAttribute("open");
    fireEvent.click(detailsSummary);
    expect(details).toHaveAttribute("open");
    expect(within(details).getByText("region-hash-a")).toBeInTheDocument();
    expect(within(details).getByText("Dirty working tree")).toBeInTheDocument();
    expect(within(details).getByText("stored-checksum-a")).toBeInTheDocument();
    expect(within(details).getByText("Captured bytes are not marked stale"))
      .toBeInTheDocument();
    expect(within(details).getByText("https://github.com/example/research.git"))
      .toBeInTheDocument();
    expect(details).not.toHaveTextContent("ghp_secret");
    expect(details).not.toHaveTextContent("also-secret");

    const expand = within(proposalA).getByRole("button", { name: "Expand panel-a.png" });
    expect(expand).toHaveAttribute("aria-expanded", "false");
    fireEvent.click(expand);
    expect(within(proposalA).getByRole("button", { name: "Collapse panel-a.png" }))
      .toHaveAttribute("aria-expanded", "true");

    fireEvent.click(within(proposalA).getByRole("button", { name: "Accept" }));
    await waitFor(() => expect(within(proposalA).getByText("accepted")).toBeInTheDocument());
    expect(rawCalls).toEqual({ [figureA]: 1, [figureB]: 1 });
  });

  it("shows ambiguous legacy figures once and omits unrelated source artifacts", async () => {
    const draft = draftFixture();
    const figureId = "figure-note";
    const audioId = "audio-note";
    const ambiguousDraft = {
      ...draft,
      context_packet: {
        source_artifacts: [
          {
            content_type: "image/png",
            filename: "legacy-figure.png",
            metadata: {},
            note_id: figureId,
            type: "image",
          },
          {
            content_type: "audio/webm",
            filename: "voice.webm",
            metadata: {},
            note_id: audioId,
            type: "audio",
          },
        ],
      },
      operations: [
        {
          ...draft.operations[0],
          source_refs: [
            {
              label: "legacy capture bundle",
              source_note_ids: [figureId, audioId],
            },
          ],
        },
      ],
      source_content_type: "image/png",
      source_note_id: figureId,
      source_note_ids: [figureId, audioId],
    };
    let rawCalls = 0;
    URL.createObjectURL = vi.fn(() => "blob:legacy-figure");
    URL.revokeObjectURL = vi.fn();

    renderDraft(ambiguousDraft, {
      routes: [
        {
          match: `/notes/${figureId}/raw`,
          response: () => {
            rawCalls += 1;
            return binaryResponse({ body: "figure", contentType: "image/png" });
          },
        },
      ],
    });

    const sharedHeading = await screen.findByText("Shared source evidence");
    const sharedEvidence = sharedHeading.closest("section");
    expect(await within(sharedEvidence).findByRole("img", {
      name: "Figure evidence: legacy-figure.png",
    })).toBeInTheDocument();
    expect(within(sharedEvidence).queryByText("voice.webm")).not.toBeInTheDocument();
    expect(rawCalls).toBe(1);

    const proposal = screen
      .getByText("Does sleep change courtship behavior?", {
        selector: ".review-proposal-text",
      })
      .closest(".review-proposal");
    expect(within(proposal).getByText("See shared source evidence above."))
      .toBeInTheDocument();
    expect(within(proposal).queryByText("Figure evidence")).not.toBeInTheDocument();
  });

  it("honors explicit ambiguity with one candidate and explains unavailable revision attachments", async () => {
    const draft = draftFixture();
    const figureId = "only-candidate-figure";
    const message =
      "Reviewer attachment previews are unavailable because revision attachments are not persisted.";
    const ambiguousDraft = {
      ...draft,
      context_packet: {
        review_attachment_evidence: {
          attachment_labels: ["corrected.png (image/png)"],
          message,
          reason: "revision_attachments_not_persisted",
          status: "unavailable",
        },
        source_artifacts: [
          {
            content_type: "image/png",
            filename: "original-figure.png",
            metadata: { evidence_capture_kind: "figure" },
            note_id: figureId,
            type: "image",
          },
        ],
      },
      operations: [
        {
          ...draft.operations[0],
          source_refs: [
            {
              label: "candidate source",
              source_note_ids: [figureId],
              source_note_ids_resolution: "ambiguous_bundle",
            },
          ],
        },
      ],
      source_content_type: "image/png",
      source_note_id: figureId,
      source_note_ids: [figureId],
    };
    URL.createObjectURL = vi.fn(() => "blob:only-candidate");
    URL.revokeObjectURL = vi.fn();

    renderDraft(ambiguousDraft, {
      routes: [
        {
          match: `/notes/${figureId}/raw`,
          response: binaryResponse({ body: "figure", contentType: "image/png" }),
        },
      ],
    });

    const sharedHeading = await screen.findByText("Shared source evidence");
    const sharedEvidence = sharedHeading.closest("section");
    expect(within(sharedEvidence).getByText(message)).toBeInTheDocument();
    expect(await within(sharedEvidence).findByRole("img", {
      name: "Figure evidence: original-figure.png",
    })).toBeInTheDocument();

    const proposal = screen
      .getByText("Does sleep change courtship behavior?", {
        selector: ".review-proposal-text",
      })
      .closest(".review-proposal");
    expect(within(proposal).getByText("See shared source evidence above."))
      .toBeInTheDocument();
    expect(within(proposal).queryByText("Figure evidence")).not.toBeInTheDocument();
  });

  it("bounds concurrent figure requests while loading a large evidence bundle", async () => {
    const draft = draftFixture();
    const figureIds = Array.from({ length: 6 }, (_, index) => `figure-${index}`);
    const resolvers = new Map();
    const startedIds = [];
    const routes = figureIds.map((figureId) => ({
      match: `/notes/${figureId}/raw`,
      response: () =>
        new Promise((resolve) => {
          startedIds.push(figureId);
          resolvers.set(figureId, resolve);
        }),
    }));
    const evidenceDraft = {
      ...draft,
      context_packet: {
        source_artifacts: figureIds.map((figureId, index) => ({
          content_type: "image/png",
          filename: `figure-${index}.png`,
          metadata: { evidence_capture_kind: "figure" },
          note_id: figureId,
          type: "image",
        })),
      },
      operations: [
        {
          ...draft.operations[0],
          source_refs: [
            {
              label: "all panels",
              source_note_ids: figureIds,
              source_note_ids_resolution: "explicit",
            },
          ],
        },
      ],
      source_note_ids: figureIds,
    };
    let objectUrlIndex = 0;
    URL.createObjectURL = vi.fn(() => `blob:bounded-${(objectUrlIndex += 1)}`);
    URL.revokeObjectURL = vi.fn();

    renderDraft(evidenceDraft, { routes });

    await screen.findByText("Does sleep change courtship behavior?", {
      selector: ".review-proposal-text",
    });
    await waitFor(() => expect(startedIds).toHaveLength(4));
    expect(startedIds).toEqual(figureIds.slice(0, 4));

    resolvers.get(figureIds[0])(
      binaryResponse({ body: "figure-0", contentType: "image/png" })
    );
    await waitFor(() => expect(startedIds).toHaveLength(5));
    resolvers.get(figureIds[1])(
      binaryResponse({ body: "figure-1", contentType: "image/png" })
    );
    await waitFor(() => expect(startedIds).toHaveLength(6));

    for (const figureId of figureIds.slice(2)) {
      resolvers.get(figureId)(
        binaryResponse({ body: figureId, contentType: "image/png" })
      );
    }
    await waitFor(() =>
      expect(screen.getAllByRole("img", { name: /Figure evidence:/ })).toHaveLength(6)
    );
  });

  it("explains pointer-only, stale, failed, and missing figure evidence", async () => {
    const draft = draftFixture();
    const pointerId = "pointer-note";
    const failedId = "failed-note";
    const missingId = "missing-note";
    const stateDraft = {
      ...draft,
      context_packet: {
        source_artifacts: [
          {
            content_type: "text/plain",
            filename: "oversize-figure.png",
            metadata: {
              evidence_capture_kind: "figure",
              evidence_source_uri: "file:///figures/oversize-figure.png",
              figure_no_preview: true,
              figure_review_bytes_stale: true,
            },
            note_id: pointerId,
            type: "file",
          },
          {
            content_type: "image/png",
            filename: "unavailable-figure.png",
            metadata: { evidence_capture_kind: "figure" },
            note_id: failedId,
            type: "image",
          },
          {
            content_type: "audio/webm",
            filename: "unrelated-audio.webm",
            metadata: {},
            note_id: "audio-note",
            type: "audio",
          },
        ],
      },
      operations: [
        {
          ...draft.operations[0],
          source_refs: [
            {
              label: "figure sources",
              source_note_ids: [pointerId, failedId, missingId],
              source_note_ids_resolution: "explicit",
            },
          ],
        },
      ],
      source_content_type: "",
      source_note_id: null,
      source_note_ids: [pointerId, failedId, missingId],
    };
    URL.createObjectURL = vi.fn();
    URL.revokeObjectURL = vi.fn();

    renderDraft(stateDraft, {
      routes: [
        {
          match: `/notes/${failedId}/raw`,
          response: errorResponse("asset unavailable", 503),
        },
      ],
    });

    expect(await screen.findByText("Preview unavailable — only a file pointer was captured."))
      .toBeInTheDocument();
    expect(screen.getByText(/Preview may be stale/)).toBeInTheDocument();
    expect(screen.getAllByText("file:///figures/oversize-figure.png")).toHaveLength(2);
    expect(await screen.findByText(/Figure preview could not be loaded/)).toBeInTheDocument();
    expect(screen.getByText("Source capture metadata is unavailable for this reference."))
      .toBeInTheDocument();
    expect(screen.getByText("Capture metadata unavailable")).toBeInTheDocument();

    const proposal = screen
      .getByText("Does sleep change courtship behavior?", {
        selector: ".review-proposal-text",
      })
      .closest(".review-proposal");
    expect(within(proposal).queryByText("unrelated-audio.webm")).not.toBeInTheDocument();
  });
});

describe("GraphDraftDetailCard audio review", () => {
  it("plays, pauses, resumes, stops, and cancels narration on navigation", async () => {
    const speechSynthesis = installSpeechSynthesis();
    const draft = draftFixture();
    const { unmount } = renderDraft(draft);

    fireEvent.click(await screen.findByRole("button", { name: "Listen to review" }));
    expect(speechSynthesis.speak).toHaveBeenCalledTimes(1);
    expect(speechSynthesis.speak.mock.calls[0][0].text).toContain(
      "Does sleep change courtship behavior?"
    );

    fireEvent.click(screen.getByRole("button", { name: "Pause audio review" }));
    expect(speechSynthesis.pause).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole("button", { name: "Resume audio review" }));
    expect(speechSynthesis.resume).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole("button", { name: "Stop audio" }));
    expect(speechSynthesis.cancel).toHaveBeenCalledTimes(2);

    fireEvent.click(screen.getByRole("button", { name: "Listen to review" }));
    unmount();
    expect(speechSynthesis.cancel).toHaveBeenCalledTimes(4);
  });

  it("records, previews, and submits voice feedback through the revision endpoint", async () => {
    installSpeechSynthesis();
    const draft = draftFixture({ draft_mode: "graph_context" });
    const track = { stop: vi.fn() };
    Object.defineProperty(navigator, "mediaDevices", {
      configurable: true,
      value: { getUserMedia: vi.fn().mockResolvedValue({ getTracks: () => [track] }) },
    });
    URL.createObjectURL = vi.fn(() => "blob:recorded-feedback");
    URL.revokeObjectURL = vi.fn();

    class FakeMediaRecorder {
      static isTypeSupported() {
        return true;
      }

      constructor(_stream, options = {}) {
        this.listeners = {};
        this.mimeType = options.mimeType || "audio/webm";
        this.state = "inactive";
      }

      addEventListener(name, callback) {
        this.listeners[name] = callback;
      }

      start() {
        this.state = "recording";
      }

      stop() {
        this.state = "inactive";
        this.listeners.dataavailable?.({
          data: new Blob(["spoken correction"], { type: this.mimeType }),
        });
        this.listeners.stop?.();
      }
    }
    vi.stubGlobal("MediaRecorder", FakeMediaRecorder);

    let revisionPayload = null;
    const { unmount } = renderDraft(draft, {
      routes: [
        {
          match: `/graph-drafts/${draft.change_set_id}/revise`,
          method: "POST",
          response: (request) => {
            revisionPayload = request.init.body;
            return apiResponse({ ...draft, summary: "Revised from voice feedback." });
          },
        },
      ],
    });

    fireEvent.click(await screen.findByRole("button", { name: "Dictate feedback" }));
    fireEvent.click(await screen.findByRole("button", { name: "Stop recording" }));

    expect(await screen.findByLabelText("Recorded feedback preview")).toHaveAttribute(
      "src",
      "blob:recorded-feedback"
    );
    expect(track.stop).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole("button", { name: "Revise with AI" }));

    await waitFor(() => expect(revisionPayload).toBeInstanceOf(FormData));
    const recording = revisionPayload.get("audio");
    expect(recording).toBeInstanceOf(File);
    expect(recording.name).toBe("dictated-feedback.webm");

    fireEvent.click(await screen.findByRole("button", { name: "Dictate feedback" }));
    await screen.findByRole("button", { name: "Stop recording" });
    unmount();
    expect(track.stop).toHaveBeenCalledTimes(2);
  });

  it("can dictate again after the recorder fails to start", async () => {
    installSpeechSynthesis();
    const draft = draftFixture({ draft_mode: "graph_context" });
    const track = { stop: vi.fn() };
    const getUserMedia = vi.fn().mockResolvedValue({ getTracks: () => [track] });
    Object.defineProperty(navigator, "mediaDevices", {
      configurable: true,
      value: { getUserMedia },
    });
    let startAttempts = 0;

    class FlakyMediaRecorder {
      static isTypeSupported() {
        return true;
      }

      constructor(_stream, options = {}) {
        this.listeners = {};
        this.mimeType = options.mimeType || "audio/webm";
        this.state = "inactive";
      }

      addEventListener(name, callback) {
        this.listeners[name] = callback;
      }

      start() {
        startAttempts += 1;
        if (startAttempts === 1) {
          throw new DOMException("The stream is inactive.", "InvalidStateError");
        }
        this.state = "recording";
      }

      stop() {
        this.state = "inactive";
        this.listeners.stop?.();
      }
    }
    vi.stubGlobal("MediaRecorder", FlakyMediaRecorder);
    const setFlash = vi.fn();

    renderDraft(draft, { setFlash });

    fireEvent.click(await screen.findByRole("button", { name: "Dictate feedback" }));
    await waitFor(() =>
      expect(setFlash).toHaveBeenLastCalledWith(
        "",
        "Could not start recording: The stream is inactive."
      )
    );
    expect(track.stop).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByRole("button", { name: "Dictate feedback" }));

    expect(await screen.findByRole("button", { name: "Stop recording" })).toBeInTheDocument();
    expect(getUserMedia).toHaveBeenCalledTimes(2);
  });

  it("blames microphone permissions only when the microphone was refused", async () => {
    installSpeechSynthesis();
    const draft = draftFixture({ draft_mode: "graph_context" });
    Object.defineProperty(navigator, "mediaDevices", {
      configurable: true,
      value: {
        getUserMedia: vi
          .fn()
          .mockRejectedValue(new DOMException("Permission denied", "NotAllowedError")),
      },
    });
    vi.stubGlobal(
      "MediaRecorder",
      class {
        static isTypeSupported() {
          return true;
        }
      }
    );
    const setFlash = vi.fn();

    renderDraft(draft, { setFlash });

    fireEvent.click(await screen.findByRole("button", { name: "Dictate feedback" }));
    await waitFor(() =>
      expect(setFlash).toHaveBeenLastCalledWith(
        "",
        "Could not access the microphone. Check browser permissions."
      )
    );
    expect(screen.getByRole("button", { name: "Dictate feedback" })).toBeInTheDocument();
  });
});

describe("GraphDraftDetailCard keyboard review", () => {
  function twoProposalDraft() {
    const first = draftFixture().operations[0];
    return draftFixture({
      operations: [
        first,
        {
          ...first,
          operation_id: "44444444-4444-4444-8444-444444444444",
          payload: { text: "Does temperature change courtship?" },
        },
      ],
    });
  }

  function installDecisionRoute(draft, patchBodies) {
    let currentDraft = draft;
    return {
      match: /\/graph-drafts\/[^/]+\/operations\/([^/]+)$/,
      method: "PATCH",
      response: (request) => {
        const operationId = request.url.split("/").pop();
        const body = JSON.parse(request.init.body);
        patchBodies.push({ operationId, ...body });
        currentDraft = {
          ...currentDraft,
          operations: currentDraft.operations.map((operation) =>
            operation.operation_id === operationId
              ? {
                  ...operation,
                  ...(body.payload ? { payload: body.payload } : {}),
                  ...(body.status ? { status: body.status } : {}),
                  ...(body.deferred ? { deferred_at: "2026-07-15T21:00:00Z" } : {}),
                }
              : operation
          ),
        };
        return apiResponse(currentDraft);
      },
    };
  }

  it("moves through proposals with j/k, accepts with a, defers with d, and rejects with r plus a digit", async () => {
    const draft = twoProposalDraft();
    const [first, second] = draft.operations;
    const patchBodies = [];
    const setFlash = vi.fn();
    const { container } = renderDraft(draft, {
      routes: [installDecisionRoute(draft, patchBodies)],
      setFlash,
    });
    await screen.findAllByText("Does sleep change courtship behavior?");
    expect(screen.getByLabelText("Keyboard shortcuts")).toBeInTheDocument();
    const row = (operation) => container.querySelector(`#review-op-${operation.operation_id}`);

    fireEvent.keyDown(document, { key: "j" });
    expect(row(first)).toHaveAttribute("aria-current", "true");
    expect(row(second)).not.toHaveAttribute("aria-current");

    // d defers immediately: one stamp, no reason, the proposal stays proposed.
    fireEvent.keyDown(document, { key: "d" });
    await waitFor(() => expect(patchBodies).toHaveLength(1));
    expect(patchBodies[0]).toEqual({ operationId: first.operation_id, deferred: true });
    await waitFor(() =>
      expect(setFlash).toHaveBeenLastCalledWith("Deferred: Does sleep change courtship behavior?")
    );
    // A deferred proposal is no longer "next": focus moves to the undecided one.
    await waitFor(() => expect(row(second)).toHaveAttribute("aria-current", "true"));
    expect(within(row(first)).getByText("deferred")).toBeInTheDocument();

    fireEvent.keyDown(document, { key: "a" });
    await waitFor(() => expect(patchBodies).toHaveLength(2));
    expect(patchBodies[1]).toMatchObject({ operationId: second.operation_id, status: "accepted" });
    await waitFor(() =>
      expect(setFlash).toHaveBeenLastCalledWith("Accepted: Does temperature change courtship?")
    );

    // r asks for a reason; Escape backs out without a request.
    fireEvent.keyDown(document, { key: "k" });
    expect(row(first)).toHaveAttribute("aria-current", "true");
    fireEvent.keyDown(document, { key: "r" });
    const chips = await screen.findByRole("group", { name: "Reason" });
    expect(within(chips).getAllByRole("button")).toHaveLength(REJECT_REASONS.length + 1);
    fireEvent.keyDown(document, { key: "Escape" });
    await waitFor(() =>
      expect(screen.queryByRole("group", { name: "Reason" })).not.toBeInTheDocument()
    );
    expect(patchBodies).toHaveLength(2);

    // r then a digit writes the rejection with that structured reason.
    fireEvent.keyDown(document, { key: "r" });
    await screen.findByRole("group", { name: "Reason" });
    fireEvent.keyDown(document, { key: "3" });
    await waitFor(() => expect(patchBodies).toHaveLength(3));
    expect(patchBodies[2]).toMatchObject({
      operationId: first.operation_id,
      reject_reason: REJECT_REASONS[2].value,
      status: "rejected",
    });
    expect(patchBodies[2].reject_reason).toBe("unsupported_by_source");
    await waitFor(() =>
      expect(setFlash).toHaveBeenLastCalledWith("Rejected: Does sleep change courtship behavior?")
    );
    expect(screen.getByText(/1 rejected · 0 undecided/)).toBeInTheDocument();
  });

  it("asks for a reason from the Reject button and writes the chosen chip", async () => {
    const draft = draftFixture();
    const patchBodies = [];
    renderDraft(draft, { routes: [installDecisionRoute(draft, patchBodies)] });
    await screen.findAllByText("Does sleep change courtship behavior?");

    fireEvent.click(screen.getByRole("button", { name: "Reject" }));
    const chips = await screen.findByRole("group", { name: "Reason" });
    fireEvent.click(within(chips).getByRole("button", { name: /Not relevant/ }));
    await waitFor(() => expect(patchBodies).toHaveLength(1));
    expect(patchBodies[0]).toMatchObject({ reject_reason: "not_relevant", status: "rejected" });
    await waitFor(() =>
      expect(screen.queryByRole("group", { name: "Reason" })).not.toBeInTheDocument()
    );
  });

  it("leaves the shortcuts alone while a field is being typed in", async () => {
    const draft = twoProposalDraft();
    const patchBodies = [];
    renderDraft(draft, { routes: [installDecisionRoute(draft, patchBodies)] });
    await screen.findAllByText("Does sleep change courtship behavior?");

    fireEvent.keyDown(document, { key: "j" });
    const [note] = screen.getAllByLabelText("Decision note");
    note.focus();
    fireEvent.keyDown(note, { key: "a" });
    fireEvent.keyDown(note, { key: "j" });
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(patchBodies).toHaveLength(0);
  });

  it("offers Defer from the narrative view too", async () => {
    const draft = draftFixture();
    const patchBodies = [];
    renderDraft(draft, { routes: [installDecisionRoute(draft, patchBodies)] });
    await screen.findAllByText("Does sleep change courtship behavior?");

    fireEvent.click(screen.getByRole("button", { name: "Narrative" }));
    fireEvent.click(screen.getByRole("button", { name: /Proposed edit 1:/ }));
    fireEvent.click(await screen.findByRole("button", { name: "Defer edit" }));
    await waitFor(() => expect(patchBodies).toHaveLength(1));
    expect(patchBodies[0]).toEqual({ operationId: draft.operations[0].operation_id, deferred: true });
  });

  it("shows one advisory line from the project's draft quality ledger", async () => {
    const draft = draftFixture({ prompt_version: "daily-batch-graph-draft-v7" });
    renderDraft(draft, {
      routes: [
        {
          match: "/projects/project-1/draft-quality",
          response: apiResponse({
            cells: [
              {
                accepted_total: 31,
                model: "gpt-5.4-mini",
                prompt_version: "daily-batch-graph-draft-v7",
                proposed: 40,
                provider: "openai",
                rejected: 6,
              },
              {
                accepted_total: 1,
                model: "other-model",
                prompt_version: "daily-batch-graph-draft-v7",
                proposed: 9,
                provider: "openai",
                rejected: 8,
              },
            ],
            change_set_count: 13,
            groups: [
              {
                change_set_count: 12,
                change_sets_with_clarifications: 3,
                model: "gpt-5.4-mini",
                prompt_version: "daily-batch-graph-draft-v7",
                provider: "openai",
              },
              {
                change_set_count: 1,
                change_sets_with_clarifications: 0,
                model: "other-model",
                prompt_version: "daily-batch-graph-draft-v7",
                provider: "openai",
              },
            ],
            project_id: "project-1",
          }),
        },
      ],
    });

    expect(await screen.findByRole("status", { name: "" })).toHaveTextContent(
      "Across 12 earlier reviews from openai/gpt-5.4-mini (daily-batch-graph-draft-v7) you kept " +
        "31 of 40 proposals and rejected 6. 3 reviews asked you for clarification."
    );
  });

  it("lets the reviewer set a source note aside with a reason", async () => {
    const draft = draftFixture();
    const noteId = draft.source_note_id;
    const archiveBodies = [];
    const setFlash = vi.fn();
    renderDraft(draft, {
      routes: [
        {
          match: `/notes/${noteId}/archive`,
          method: "POST",
          response: (request) => {
            archiveBodies.push(JSON.parse(request.init.body));
            return apiResponse({ note_id: noteId, status: "archived" });
          },
        },
      ],
      setFlash,
    });
    await screen.findAllByText("Does sleep change courtship behavior?");

    fireEvent.change(screen.getByLabelText(`Set-aside reason for ${noteId}`), {
      target: { value: "superseded" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Set aside" }));

    await waitFor(() => expect(archiveBodies).toEqual([{ reason: "superseded" }]));
    await waitFor(() =>
      expect(setFlash).toHaveBeenLastCalledWith("Capture set aside (superseded).")
    );
  });

  it("accepts and rejects proposed provenance links from the review page", async () => {
    const draft = draftFixture();
    const linkBodies = [];
    const link = {
      basis: "content_hash_match",
      content_hash: "sha256:abc",
      link_id: "link-1",
      project_id: "project-1",
      relation: "was_derived_from",
      source: { entity_id: "note-b", entity_type: "note" },
      status: "proposed",
      target: { entity_id: "note-a", entity_type: "note" },
    };
    renderDraft(draft, {
      routes: [
        {
          match: /^\/provenance-links\?/,
          response: apiResponse([link]),
        },
        {
          match: "/provenance-links/link-1",
          method: "PATCH",
          response: (request) => {
            linkBodies.push(JSON.parse(request.init.body));
            return apiResponse({ ...link, status: "accepted" });
          },
        },
      ],
    });

    const section = await screen.findByRole("region", { name: "Proposed provenance links" });
    expect(within(section).getByText("sha256:abc")).toBeInTheDocument();
    fireEvent.click(within(section).getByRole("button", { name: "Accept" }));

    await waitFor(() => expect(linkBodies).toEqual([{ status: "accepted" }]));
    await waitFor(() =>
      expect(
        screen.queryByRole("region", { name: "Proposed provenance links" })
      ).not.toBeInTheDocument()
    );
  });

  it("gives claim statements and falsification criteria typed editors", async () => {
    const draft = draftFixture({
      operations: [
        {
          ...draftFixture().operations[0],
          entity_type: "claim",
          semantic_type: null,
          payload: {
            statement: "Sleep loss reduces courtship",
            falsification_criteria: "No change in courtship index",
            primary_question_id: "q-1",
          },
        },
      ],
    });
    renderDraft(draft);
    await screen.findAllByText("Sleep loss reduces courtship");

    const statement = screen.getByLabelText("Statement");
    expect(statement.tagName).toBe("TEXTAREA");
    expect(screen.getByLabelText("Falsification criteria")).toHaveValue(
      "No change in courtship index"
    );
    expect(screen.queryByLabelText("Primary question id")).not.toBeInTheDocument();

    fireEvent.change(statement, { target: { value: "Sleep loss halves courtship" } });
    expect(JSON.parse(screen.getByLabelText("Edit JSON payload").value)).toMatchObject({
      statement: "Sleep loss halves courtship",
      falsification_criteria: "No change in courtship index",
      primary_question_id: "q-1",
    });
  });
});

describe("GraphDraftDetailCard commit hand-off", () => {
  it("commits with a suggested message when none is typed and offers the next review", async () => {
    const draft = draftFixture({
      operations: [{ ...draftFixture().operations[0], status: "accepted" }],
    });
    let commitBody = null;
    const navigate = vi.fn();
    renderDraft(draft, {
      navigate,
      routes: [
        {
          match: `/graph-drafts/${draft.change_set_id}/commit`,
          method: "POST",
          response: (request) => {
            commitBody = JSON.parse(request.init.body);
            return apiResponse({ ...draft, status: "committed" });
          },
        },
        {
          match: "/batches?limit=5&mine=true",
          response: apiResponse([
            { change_set_id: draft.change_set_id, status: "committed" },
            { change_set_id: "55555555-5555-4555-8555-555555555555", status: "ready" },
          ]),
        },
      ],
    });
    await screen.findByText("1 of 1 kept");
    const commitField = screen.getByLabelText(/^Commit message/);
    expect(commitField).toHaveAttribute(
      "placeholder",
      "Daily review 2026-07-15: kept 1 of 1 proposals"
    );

    fireEvent.click(screen.getByRole("button", { name: "Commit accepted changes" }));
    await waitFor(() => expect(commitBody).not.toBeNull());
    expect(commitBody.message).toBe("Daily review 2026-07-15: kept 1 of 1 proposals");

    expect(await screen.findByText("Committed.")).toBeInTheDocument();
    fireEvent.click(await screen.findByRole("button", { name: "Next review" }));
    expect(navigate).toHaveBeenCalledWith("/app/batches/55555555-5555-4555-8555-555555555555");
  });
});

