import * as React from "react";

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { GraphDraftDetailCard, spokenReviewScript } from "./graph-drafts.jsx";
import { apiResponse, errorResponse, installFetchMock } from "../test/utils.js";

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

  function installAcceptAll(
    draft,
    { acceptAllResponse, patchResponse = null, setFlash = vi.fn() } = {}
  ) {
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
            return apiResponse(patchResponse ? patchResponse(request) : draft);
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

  describe("undo", () => {
    const HAND_ID = "55555555-5555-4555-8555-555555555555";
    const BULK_ID = "66666666-6666-4666-8666-666666666666";
    const INVALID_ID = "77777777-7777-4777-8777-777777777777";

    // One operation of each kind the undo must tell apart.
    function mixedDraft() {
      const base = draftFixture();
      const template = base.operations[0];
      return {
        ...base,
        operations: [
          {
            ...template,
            acceptance_mode: "human_selected",
            operation_id: HAND_ID,
            payload: { text: "Scrutinized by hand" },
            status: "accepted",
          },
          {
            ...template,
            operation_id: BULK_ID,
            payload: { text: "Swept up in the batch" },
            status: "proposed",
          },
          {
            ...template,
            operation_id: INVALID_ID,
            payload: { text: "Fails patch validation" },
            status: "proposed",
          },
        ],
      };
    }

    // What the endpoint returns: the hand-accepted op keeps its mark, one op is
    // newly bulk-accepted, and the invalid one is left proposed for editing.
    function mixedAccepted(draft) {
      return {
        ...draft,
        operations: [
          draft.operations[0],
          { ...draft.operations[1], acceptance_mode: "bulk_accepted", status: "accepted" },
          draft.operations[2],
        ],
      };
    }

    async function acceptAllThen(draft, options = {}) {
      const handles = installAcceptAll(draft, {
        acceptAllResponse: mixedAccepted(draft),
        ...options,
      });
      fireEvent.click(await screen.findByRole("button", { name: "Accept all" }));
      await waitFor(() => expect(handles.acceptAllCalls).toHaveLength(1));
      return handles;
    }

    it("offers undo scoped to only the operations the batch flipped", async () => {
      await acceptAllThen(mixedDraft());

      expect(await screen.findByText("1 proposal accepted as a batch.")).toBeInTheDocument();
      expect(screen.getByRole("button", { name: "Undo accept all" })).toBeInTheDocument();
    });

    it("reverts exactly the bulk-flipped operation, not the hand-accepted one", async () => {
      const draft = mixedDraft();
      const { patchCalls } = await acceptAllThen(draft, {
        patchResponse: () => ({
          ...draft,
          operations: draft.operations.map((operation) => ({ ...operation })),
        }),
      });

      fireEvent.click(screen.getByRole("button", { name: "Undo accept all" }));

      await waitFor(() => expect(patchCalls).toHaveLength(1));
      // Only the bulk-flipped operation; the hand-accepted one keeps its
      // human_selected mark and the invalid one was never accepted.
      expect(patchCalls[0].url).toContain(BULK_ID);
      expect(patchCalls[0].url).not.toContain(HAND_ID);
      expect(patchCalls[0].url).not.toContain(INVALID_ID);
      // Status-only, so the payload and decision note are left untouched and
      // re-opening clears the acceptance stamp.
      expect(JSON.parse(patchCalls[0].init.body)).toEqual({ status: "proposed" });
    });

    it("retires the offer once the operations are no longer accepted", async () => {
      const draft = mixedDraft();
      await acceptAllThen(draft, {
        patchResponse: () => draft,
      });

      fireEvent.click(screen.getByRole("button", { name: "Undo accept all" }));

      await waitFor(() =>
        expect(screen.queryByRole("button", { name: "Undo accept all" })).toBeNull()
      );
    });

    it("drops an operation from the undo set once it is hand-accepted afterwards", async () => {
      const draft = mixedDraft();
      const accepted = mixedAccepted(draft);
      renderDraft(draft, {
        routes: [
          {
            match: `/graph-drafts/${draft.change_set_id}/accept-all`,
            method: "POST",
            response: apiResponse(accepted),
          },
          {
            match: /\/graph-drafts\/[^/]+\/operations\//,
            method: "PATCH",
            // Scrutinizing the batch-accepted op re-stamps it human_selected.
            response: apiResponse({
              ...accepted,
              operations: [
                accepted.operations[0],
                { ...accepted.operations[1], acceptance_mode: "human_selected" },
                accepted.operations[2],
              ],
            }),
          },
        ],
      });

      fireEvent.click(await screen.findByRole("button", { name: "Accept all" }));
      await screen.findByRole("button", { name: "Undo accept all" });

      // Accept that one by hand — a real decision undo must not throw away.
      fireEvent.click(screen.getAllByRole("button", { name: "Accept" })[1]);

      await waitFor(() =>
        expect(screen.queryByRole("button", { name: "Undo accept all" })).toBeNull()
      );
    });

    it("ignores an operation accepted elsewhere while this client's view was stale", async () => {
      const draft = mixedDraft();
      // This client still shows it proposed, but it was hand-accepted from the
      // phone; accept-all skipped it and the response says human_selected.
      const accepted = {
        ...draft,
        operations: [
          draft.operations[0],
          { ...draft.operations[1], acceptance_mode: "human_selected", status: "accepted" },
          { ...draft.operations[2], acceptance_mode: "bulk_accepted", status: "accepted" },
        ],
      };
      const patchCalls = [];
      renderDraft(draft, {
        routes: [
          {
            match: `/graph-drafts/${draft.change_set_id}/accept-all`,
            method: "POST",
            response: apiResponse(accepted),
          },
          {
            match: /\/graph-drafts\/[^/]+\/operations\//,
            method: "PATCH",
            response: (request) => {
              patchCalls.push(request);
              return apiResponse(accepted);
            },
          },
        ],
      });

      fireEvent.click(await screen.findByRole("button", { name: "Accept all" }));
      // Only the genuinely batch-accepted one is offered.
      expect(await screen.findByText("1 proposal accepted as a batch.")).toBeInTheDocument();

      fireEvent.click(screen.getByRole("button", { name: "Undo accept all" }));

      await waitFor(() => expect(patchCalls).toHaveLength(1));
      expect(patchCalls[0].url).toContain(INVALID_ID);
      expect(patchCalls[0].url).not.toContain(BULK_ID);
    });

    it("reports how far a failed undo got and keeps the confirmed state", async () => {
      const draft = mixedDraft();
      // Two operations to revert; the second PATCH fails.
      draft.operations[2] = { ...draft.operations[2], status: "proposed" };
      const accepted = {
        ...draft,
        operations: [
          draft.operations[0],
          { ...draft.operations[1], acceptance_mode: "bulk_accepted", status: "accepted" },
          { ...draft.operations[2], acceptance_mode: "bulk_accepted", status: "accepted" },
        ],
      };
      const setFlash = vi.fn();
      let patched = 0;
      renderDraft(draft, {
        setFlash,
        routes: [
          {
            match: `/graph-drafts/${draft.change_set_id}/accept-all`,
            method: "POST",
            response: apiResponse(accepted),
          },
          {
            match: /\/graph-drafts\/[^/]+\/operations\//,
            method: "PATCH",
            response: () => {
              patched += 1;
              return patched === 1
                ? apiResponse({
                    ...accepted,
                    operations: [
                      accepted.operations[0],
                      draft.operations[1],
                      accepted.operations[2],
                    ],
                  })
                : errorResponse("Operation is locked", 409);
            },
          },
        ],
      });

      fireEvent.click(await screen.findByRole("button", { name: "Accept all" }));
      fireEvent.click(await screen.findByRole("button", { name: "Undo accept all" }));

      await waitFor(() =>
        expect(setFlash).toHaveBeenCalledWith(
          "",
          "Undo stopped after 1 of 2 proposals: Operation is locked"
        )
      );
      // The one that did revert is reflected, so the screen is not stale.
      await waitFor(() =>
        expect(screen.getByText("1 proposal accepted as a batch.")).toBeInTheDocument()
      );
    });

    it("does not offer undo when the batch flipped nothing", async () => {
      const draft = mixedDraft();
      // Everything already decided: accept-all has nothing left to flip.
      draft.operations[1] = { ...draft.operations[1], status: "rejected" };
      draft.operations[2] = { ...draft.operations[2], status: "rejected" };
      installAcceptAll(draft, { acceptAllResponse: draft });

      fireEvent.click(await screen.findByRole("button", { name: "Accept all" }));

      await waitFor(() => expect(screen.getByText(/kept/)).toBeInTheDocument());
      expect(screen.queryByRole("button", { name: "Undo accept all" })).toBeNull();
    });

    it("withdraws the offer once the draft is committed", async () => {
      const draft = mixedDraft();
      const accepted = mixedAccepted(draft);
      // Committing applies the operations, which must retire undo — this is
      // undo before commit, never a rollback of committed history.
      renderDraft(draft, {
        routes: [
          {
            match: `/graph-drafts/${draft.change_set_id}/accept-all`,
            method: "POST",
            response: apiResponse(accepted),
          },
          {
            match: `/graph-drafts/${draft.change_set_id}/commit`,
            method: "POST",
            response: apiResponse({
              ...accepted,
              operations: accepted.operations.map((operation) => ({
                ...operation,
                status: "applied",
              })),
              status: "committed",
            }),
          },
        ],
      });

      fireEvent.click(await screen.findByRole("button", { name: "Accept all" }));
      await screen.findByRole("button", { name: "Undo accept all" });

      fireEvent.change(screen.getByLabelText("Commit message"), {
        target: { value: "Commit the batch" },
      });
      fireEvent.click(screen.getByRole("button", { name: "Commit accepted changes" }));

      await waitFor(() =>
        expect(screen.queryByRole("button", { name: "Undo accept all" })).toBeNull()
      );
    });
  });
});

describe("GraphDraftDetailCard draft recovery", () => {
  const OPERATION_ID = "33333333-3333-4333-8333-333333333333";

  afterEach(() => {
    window.localStorage.clear();
  });

  it("offers editor text left behind by a closed tab, and never resubmits it", async () => {
    const draft = draftFixture();
    const editedPayload = JSON.stringify({ text: "Half-rewritten question" }, null, 2);
    window.localStorage.setItem(
      `lab-tracker:draft:graph-draft:${draft.change_set_id}`,
      JSON.stringify({
        savedAt: 1737000000000,
        value: JSON.stringify([[OPERATION_ID, editedPayload, "Checked the raw trace"]]),
      })
    );
    const fetchMock = installFetchMock([
      { match: `/graph-drafts/${draft.change_set_id}`, response: apiResponse(draft) },
    ]);
    render(
      <GraphDraftDetailCard
        token="token-1"
        changeSetId={draft.change_set_id}
        navigate={vi.fn()}
        canWrite={true}
        canManageGraph={true}
        user={{ role: "admin", user_id: "user-1", username: "sam" }}
        setBusy={vi.fn()}
        setFlash={vi.fn()}
      />
    );

    // Offered, not applied: the stored text may be older than the server's.
    expect(await screen.findByRole("button", { name: "Restore them" })).toBeInTheDocument();
    expect(screen.getByLabelText("Edit JSON payload")).toHaveValue(
      JSON.stringify(draft.operations[0].payload, null, 2)
    );

    fireEvent.click(screen.getByRole("button", { name: "Restore them" }));

    expect(screen.getByLabelText("Edit JSON payload")).toHaveValue(editedPayload);
    expect(screen.getByLabelText("Decision note")).toHaveValue("Checked the raw trace");
    // A restored payload lands in the editor only. Nothing is sent on the
    // person's behalf, so half-edited JSON is never auto-submitted.
    const writes = fetchMock.mock.calls.filter(
      ([, init]) => init && init.method && init.method !== "GET"
    );
    expect(writes).toHaveLength(0);
  });

  it("does not offer recovery once the draft can no longer be edited", async () => {
    const draft = { ...draftFixture(), status: "committed" };
    window.localStorage.setItem(
      `lab-tracker:draft:graph-draft:${draft.change_set_id}`,
      JSON.stringify({
        savedAt: 1737000000000,
        value: JSON.stringify([[OPERATION_ID, "{}", "stale"]]),
      })
    );
    installFetchMock([
      { match: `/graph-drafts/${draft.change_set_id}`, response: apiResponse(draft) },
    ]);
    render(
      <GraphDraftDetailCard
        token="token-1"
        changeSetId={draft.change_set_id}
        navigate={vi.fn()}
        canWrite={true}
        canManageGraph={true}
        user={{ role: "admin", user_id: "user-1", username: "sam" }}
        setBusy={vi.fn()}
        setFlash={vi.fn()}
      />
    );

    await screen.findByText(/kept/);
    expect(screen.queryByRole("button", { name: "Restore them" })).toBeNull();
  });
});

describe("GraphDraftDetailCard concurrent editing", () => {
  it("keeps unsaved edits on other proposals when one is saved", async () => {
    const base = draftFixture();
    const SECOND = "44444444-4444-4444-8444-444444444444";
    const draft = {
      ...base,
      operations: [
        base.operations[0],
        { ...base.operations[0], operation_id: SECOND, payload: { text: "Second proposal" } },
      ],
    };
    renderDraft(draft, {
      routes: [
        {
          match: /\/graph-drafts\/[^/]+\/operations\//,
          method: "PATCH",
          response: apiResponse({
            ...draft,
            operations: [
              { ...draft.operations[0], status: "accepted" },
              draft.operations[1],
            ],
          }),
        },
      ],
    });

    const notes = await screen.findAllByLabelText("Decision note");
    // An in-flight note on the *second* proposal, which is not being saved.
    fireEvent.change(notes[1], { target: { value: "Still thinking about this one" } });

    fireEvent.click(screen.getAllByRole("button", { name: "Accept" })[0]);

    await waitFor(() =>
      expect(screen.getAllByRole("button", { name: "Accept" })).toHaveLength(2)
    );
    // Refreshing every editor from the response would have wiped this.
    expect(screen.getAllByLabelText("Decision note")[1]).toHaveValue(
      "Still thinking about this one"
    );
  });
});
