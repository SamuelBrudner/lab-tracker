import * as React from "react";

import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { GraphDraftDetailCard, spokenReviewScript } from "./graph-drafts.jsx";
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

describe("GraphDraftDetailCard source completeness", () => {
  it("shows a prominent warning when provider context was truncated", async () => {
    renderDraft(
      draftFixture({
        source_context_truncated: true,
        context_packet: {
          context_summary: {
            warnings: [
              "source note 11111111-1111-4111-8111-111111111111 exceeded the bounded provider-context limit",
            ],
          },
        },
      })
    );

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Only part of the source context reached the drafting provider."
    );
    expect(screen.getByRole("alert")).toHaveTextContent(
      "Compare every proposal with the original source"
    );
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
    const draft = draftFixture();
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
});
