import * as React from "react";

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { GraphDraftDetailCard, spokenReviewScript } from "./graph-drafts.jsx";
import { apiResponse, installFetchMock } from "../test/utils.js";

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
