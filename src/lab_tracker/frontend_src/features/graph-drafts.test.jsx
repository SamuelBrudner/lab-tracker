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

describe("GraphDraftDetailCard accept all", () => {
  const SECOND_OPERATION_ID = "44444444-4444-4444-8444-444444444444";

  function twoOperationDraft() {
    const base = draftFixture();
    return {
      ...base,
      operations: [
        base.operations[0],
        {
          ...base.operations[0],
          operation_id: SECOND_OPERATION_ID,
          payload: { text: "Does sleep change locomotion?" },
        },
      ],
    };
  }

  function installAcceptAll(draft, { acceptAllResponse, setFlash = vi.fn() } = {}) {
    // One ordered log so tests can assert that edits are persisted *before* the
    // batch is accepted, not merely that both requests happened.
    const calls = [];
    const patchCalls = [];
    const acceptAllCalls = [];
    renderDraft(draft, {
      setFlash,
      routes: [
        {
          match: /\/graph-drafts\/[^/]+\/operations\//,
          method: "PATCH",
          response: (request) => {
            calls.push("patch");
            patchCalls.push(request);
            return apiResponse(draft);
          },
        },
        {
          match: `/graph-drafts/${draft.change_set_id}/accept-all`,
          method: "POST",
          response: (request) => {
            calls.push("accept-all");
            acceptAllCalls.push(request);
            return apiResponse(acceptAllResponse);
          },
        },
      ],
    });
    return { acceptAllCalls, calls, patchCalls, setFlash };
  }

  function allAccepted(draft) {
    return {
      ...draft,
      operations: draft.operations.map((operation) => ({
        ...operation,
        acceptance_mode: "bulk_accepted",
        status: "accepted",
      })),
    };
  }

  it("accepts through the bulk endpoint rather than looping per-operation accepts", async () => {
    const draft = twoOperationDraft();
    const { acceptAllCalls, patchCalls, setFlash } = installAcceptAll(draft, {
      acceptAllResponse: {
        ...draft,
        operations: draft.operations.map((operation) => ({
          ...operation,
          acceptance_mode: "bulk_accepted",
          status: "accepted",
        })),
      },
    });

    fireEvent.click(await screen.findByRole("button", { name: "Accept all" }));

    await waitFor(() => expect(acceptAllCalls).toHaveLength(1));
    // The per-operation PATCH records human_selected, which would represent a
    // rubber-stamped batch as scrutinized review. It must not be used here.
    expect(patchCalls).toHaveLength(0);
    await waitFor(() =>
      expect(setFlash).toHaveBeenCalledWith("Accepted all remaining proposals.")
    );
  });

  it("leaves an already hand-accepted operation for the endpoint to preserve", async () => {
    const draft = twoOperationDraft();
    draft.operations[0] = {
      ...draft.operations[0],
      acceptance_mode: "human_selected",
      status: "accepted",
    };
    const { acceptAllCalls, patchCalls } = installAcceptAll(draft, {
      acceptAllResponse: {
        ...draft,
        operations: [
          draft.operations[0],
          { ...draft.operations[1], acceptance_mode: "bulk_accepted", status: "accepted" },
        ],
      },
    });

    fireEvent.click(await screen.findByRole("button", { name: "Accept all" }));

    await waitFor(() => expect(acceptAllCalls).toHaveLength(1));
    expect(patchCalls).toHaveLength(0);
  });

  it("persists an unsaved payload edit before accepting the batch", async () => {
    const draft = draftFixture();
    const { acceptAllCalls, calls, patchCalls } = installAcceptAll(draft, {
      acceptAllResponse: allAccepted(draft),
    });

    fireEvent.change(await screen.findByLabelText("Edit JSON payload"), {
      target: { value: JSON.stringify({ text: "Edited before accepting" }, null, 2) },
    });
    fireEvent.click(screen.getByRole("button", { name: "Accept all" }));

    await waitFor(() => expect(acceptAllCalls).toHaveLength(1));
    expect(patchCalls).toHaveLength(1);
    const body = JSON.parse(patchCalls[0].init.body);
    expect(body.payload).toEqual({ text: "Edited before accepting" });
    // Saved as still-proposed so the bulk endpoint is what stamps acceptance.
    expect(body.status).toBe("proposed");
    // Order matters: persisting after the accept would not save anything.
    expect(calls).toEqual(["patch", "accept-all"]);
  });

  it("persists an unsaved decision note even when the payload is untouched", async () => {
    const draft = draftFixture();
    const { acceptAllCalls, calls, patchCalls } = installAcceptAll(draft, {
      acceptAllResponse: allAccepted(draft),
    });

    fireEvent.change(await screen.findByLabelText("Decision note"), {
      target: { value: "Checked against the raw trace." },
    });
    fireEvent.click(screen.getByRole("button", { name: "Accept all" }));

    await waitFor(() => expect(acceptAllCalls).toHaveLength(1));
    // The bulk endpoint never carries review_note, so the reviewer's own
    // rationale is lost unless it is PATCHed first.
    expect(patchCalls).toHaveLength(1);
    const body = JSON.parse(patchCalls[0].init.body);
    expect(body.review_note).toBe("Checked against the raw trace.");
    expect(body.payload).toEqual(draft.operations[0].payload);
    expect(body.status).toBe("proposed");
    expect(calls).toEqual(["patch", "accept-all"]);
  });

  it("persists every dirty operation, not just the first", async () => {
    const draft = twoOperationDraft();
    const { acceptAllCalls, calls, patchCalls } = installAcceptAll(draft, {
      acceptAllResponse: allAccepted(draft),
    });

    const editors = await screen.findAllByLabelText("Edit JSON payload");
    fireEvent.change(editors[0], {
      target: { value: JSON.stringify({ text: "First edit" }, null, 2) },
    });
    fireEvent.change(editors[1], {
      target: { value: JSON.stringify({ text: "Second edit" }, null, 2) },
    });
    fireEvent.click(screen.getByRole("button", { name: "Accept all" }));

    await waitFor(() => expect(acceptAllCalls).toHaveLength(1));
    expect(patchCalls).toHaveLength(2);
    expect(patchCalls.map((call) => JSON.parse(call.init.body).payload)).toEqual([
      { text: "First edit" },
      { text: "Second edit" },
    ]);
    expect(calls).toEqual(["patch", "patch", "accept-all"]);
  });

  it("does not PATCH when a payload is only reformatted", async () => {
    const draft = draftFixture();
    const { acceptAllCalls, patchCalls } = installAcceptAll(draft, {
      acceptAllResponse: allAccepted(draft),
    });

    fireEvent.change(await screen.findByLabelText("Edit JSON payload"), {
      target: { value: JSON.stringify(draft.operations[0].payload) },
    });
    fireEvent.click(screen.getByRole("button", { name: "Accept all" }));

    await waitFor(() => expect(acceptAllCalls).toHaveLength(1));
    expect(patchCalls).toHaveLength(0);
  });

  it("refuses to accept anything while a payload edit is invalid JSON", async () => {
    const draft = draftFixture();
    const { acceptAllCalls, patchCalls, setFlash } = installAcceptAll(draft, {
      acceptAllResponse: draft,
    });

    fireEvent.change(await screen.findByLabelText("Edit JSON payload"), {
      target: { value: "{ not valid json" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Accept all" }));

    await waitFor(() =>
      expect(setFlash).toHaveBeenCalledWith(
        "",
        "One proposal has invalid JSON. Fix or revert it before accepting all."
      )
    );
    expect(acceptAllCalls).toHaveLength(0);
    expect(patchCalls).toHaveLength(0);
  });

  it("reports operations the endpoint left proposed for editing", async () => {
    const draft = twoOperationDraft();
    const { setFlash } = installAcceptAll(draft, {
      acceptAllResponse: {
        ...draft,
        operations: [
          { ...draft.operations[0], acceptance_mode: "bulk_accepted", status: "accepted" },
          draft.operations[1],
        ],
      },
    });

    fireEvent.click(await screen.findByRole("button", { name: "Accept all" }));

    await waitFor(() =>
      expect(setFlash).toHaveBeenCalledWith(
        "Accepted all valid proposals. 1 still needs editing before it can be accepted."
      )
    );
  });
});
