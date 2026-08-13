import * as React from "react";

import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { apiResponse, errorResponse, installFetchMock } from "../test/utils.js";
import { buildApiPath } from "../shared/api.js";
import { MobileCaptureCard } from "./mobile-capture.jsx";
import { MemberOnboardingPage, OwnerOnboardingQueueBanner } from "./member-onboarding.jsx";

const PROJECT_ID = "0d637c19-0060-4dc7-8304-ef7c8c940d85";
const CHECKPOINT_ID = "9da35840-1b2c-49ef-ac7d-d9d1428dedb1";
const DRAFT_ID = "078b4b8a-9d17-4440-bbed-ec08b2be8f95";
const OPERATION_ID = "2fd31084-338f-4aca-9eb9-0c54d7942f9c";
const SHARED_QUESTION_ID = "69e9b439-2a33-47d5-8021-4e80cc41bceb";
const PATH = `/projects/${PROJECT_ID}/member-onboarding`;

function onboarding(overrides = {}) {
  return {
    alignment: { draft: null, mode: "none", question_resolutions: [], resolved_at: null },
    brief_markdown: "",
    capabilities: {
      can_align: true,
      can_capture: true,
      can_commit: false,
      can_create_checkpoint: true,
      can_read: true,
    },
    checkpoint: null,
    first_capture: null,
    guided_fields: {
      current_output_or_decision: "",
      live_questions: [],
      next_move: "",
      source_text_present: false,
      strongest_recent_context: "",
    },
    map_items: [],
    member_complete: false,
    owner_commit_pending: false,
    project_id: PROJECT_ID,
    role: "contributor",
    state: "not_started",
    ...overrides,
  };
}

function savedOnboarding(overrides = {}) {
  return onboarding({
    checkpoint: { created_at: "2026-08-13T12:00:00Z", note_id: CHECKPOINT_ID },
    guided_fields: {
      current_output_or_decision: "Choose the next control cohort",
      live_questions: ["Does the effect persist?", "Which control is decisive?"],
      next_move: "Run the control comparison",
      source_text_present: true,
      strongest_recent_context: "The pilot effect reproduced twice",
    },
    state: "checkpoint_ready",
    ...overrides,
  });
}

function renderPage({ initial = onboarding(), routes = [], ...props } = {}) {
  installFetchMock([
    { match: PATH, response: apiResponse(initial) },
    ...routes,
  ]);
  const defaults = {
    navigate: vi.fn(),
    project: { name: "Ongoing odor project", project_id: PROJECT_ID },
    projectAccess: { canContribute: true, canManage: false, role: "contributor" },
    projectId: PROJECT_ID,
    questions: [
      {
        project_id: PROJECT_ID,
        question_id: SHARED_QUESTION_ID,
        status: "active",
        text: "Which control is decisive?",
      },
    ],
    setBusy: vi.fn(),
    setFlash: vi.fn(),
    token: "token-1",
  };
  return { ...render(<MemberOnboardingPage {...defaults} {...props} />), ...defaults, ...props };
}

describe("MemberOnboardingPage", () => {
  it("saves the four-prompt checkpoint with exact request keys", async () => {
    let body;
    renderPage({
      routes: [
        {
          match: `${PATH}/checkpoint`,
          method: "PUT",
          response: (request) => {
            body = JSON.parse(request.init.body);
            return apiResponse(savedOnboarding());
          },
        },
      ],
    });

    await screen.findByRole("heading", { name: /Start tracking Ongoing odor project/ });
    fireEvent.change(screen.getByLabelText("What output or decision are you working toward now?"), { target: { value: "Choose the next control cohort" } });
    fireEvent.change(screen.getByLabelText("Question 1"), { target: { value: "Does the effect persist?" } });
    fireEvent.click(screen.getByRole("button", { name: "Add another question" }));
    fireEvent.change(screen.getByLabelText("Question 2"), { target: { value: "Which control is decisive?" } });
    fireEvent.change(screen.getByLabelText("What recent result or context matters most?"), { target: { value: "The pilot effect reproduced twice" } });
    fireEvent.change(screen.getByLabelText("What is the next move?"), { target: { value: "Run the control comparison" } });
    fireEvent.change(screen.getByLabelText("Paste a project brief, aims, or meeting context (optional)"), { target: { value: "Aim 1 context" } });
    fireEvent.click(screen.getByRole("button", { name: "Save tracking checkpoint" }));

    await waitFor(() => expect(body).toEqual({
      as_of: null,
      current_output_or_decision: "Choose the next control cohort",
      live_questions: ["Does the effect persist?", "Which control is decisive?"],
      next_move: "Run the control comparison",
      source_text: "Aim 1 context",
      strongest_recent_context: "The pilot effect reproduced twice",
    }));
    expect(await screen.findByText("The pilot effect reproduced twice")).toBeInTheDocument();
  });

  it("lets the server validate the complete rendered checkpoint length", async () => {
    const setFlash = vi.fn();
    let requestCount = 0;
    renderPage({
      routes: [{
        match: `${PATH}/checkpoint`,
        method: "PUT",
        response: () => {
          requestCount += 1;
          return errorResponse(
            "The complete tracking checkpoint must be 64000 characters or fewer; source text is never silently truncated.",
            422
          );
        },
      }],
      setFlash,
    });

    await screen.findByRole("heading", { name: /Start tracking Ongoing odor project/ });
    fireEvent.change(screen.getByLabelText("What output or decision are you working toward now?"), { target: { value: "Choose the next control cohort" } });
    fireEvent.change(screen.getByLabelText("Question 1"), { target: { value: "Does the effect persist?" } });
    fireEvent.change(screen.getByLabelText("What recent result or context matters most?"), { target: { value: "The pilot effect reproduced twice" } });
    fireEvent.change(screen.getByLabelText("What is the next move?"), { target: { value: "Run the control comparison" } });
    fireEvent.change(screen.getByLabelText("Paste a project brief, aims, or meeting context (optional)"), { target: { value: "x".repeat(64001) } });

    const save = screen.getByRole("button", { name: "Save tracking checkpoint" });
    expect(save).toBeEnabled();
    fireEvent.click(save);

    await waitFor(() => expect(requestCount).toBe(1));
    expect(setFlash).toHaveBeenLastCalledWith(
      "",
      "The complete tracking checkpoint must be 64000 characters or fewer; source text is never silently truncated."
    );
  });

  it("resumes a saved checkpoint and gates viewers to read-only access", async () => {
    renderPage({
      initial: savedOnboarding({
        capabilities: { can_align: false, can_capture: false, can_commit: false, can_create_checkpoint: false, can_read: true },
        role: "viewer",
      }),
      projectAccess: { canContribute: false, canManage: false, role: "viewer" },
    });

    expect(await screen.findByText("Choose the next control cohort")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Save tracking checkpoint" })).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Request edit access" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Make the first capture" })).toBeDisabled();
  });

  it("requires and sends one manual resolution per question with exact wire keys", async () => {
    let body;
    renderPage({
      initial: savedOnboarding(),
      routes: [
        {
          match: `${PATH}/manual-alignment`,
          method: "PUT",
          response: (request) => {
            body = JSON.parse(request.init.body);
            return apiResponse(savedOnboarding({
              alignment: { draft: null, mode: "manual", question_resolutions: body.resolutions, resolved_at: "2026-08-13T12:30:00Z" },
              state: "capture_pending",
            }));
          },
        },
      ],
    });

    fireEvent.click(await screen.findByRole("button", { name: "Align questions manually" }));
    fireEvent.change(screen.getByLabelText("Resolution for question 1"), { target: { value: "checkpoint_only" } });
    fireEvent.change(screen.getByLabelText("Resolution for question 2"), { target: { value: "link_existing" } });
    fireEvent.change(screen.getByLabelText("Shared question for question 2"), { target: { value: SHARED_QUESTION_ID } });
    fireEvent.click(screen.getByRole("button", { name: "Save each resolution" }));

    await waitFor(() => expect(body).toEqual({ resolutions: [
      { action: "checkpoint_only", question_index: 0 },
      { action: "link_existing", existing_question_id: SHARED_QUESTION_ID, question_index: 1 },
    ] }));
  });

  it("requires provider consent and per-operation review without bulk acceptance", async () => {
    const readyDraft = {
      change_set_id: DRAFT_ID,
      operations: [{ entity_type: "question", op: "create", operation_id: OPERATION_ID, payload: { text: "Does the effect persist?" }, status: "proposed" }],
      project_id: PROJECT_ID,
      purpose: "member_checkpoint_alignment",
      status: "ready",
    };
    let aiBody;
    let patchBody;
    renderPage({
      initial: savedOnboarding(),
      routes: [
        {
          match: `${PATH}/ai-alignment`, method: "POST", response: (request) => {
            aiBody = JSON.parse(request.init.body);
            return apiResponse(savedOnboarding({ alignment: { draft: readyDraft, mode: "ai", question_resolutions: [], resolved_at: null }, state: "alignment_ready" }));
          },
        },
        {
          match: `/graph-drafts/${DRAFT_ID}/operations/${OPERATION_ID}`, method: "PATCH", response: (request) => {
            patchBody = JSON.parse(request.init.body);
            return apiResponse({ ...readyDraft, operations: [{ ...readyDraft.operations[0], status: "accepted" }] });
          },
        },
        { match: `/graph-drafts/${DRAFT_ID}/submit`, method: "POST", response: apiResponse({ ...readyDraft, status: "submitted" }) },
      ],
    });

    const suggest = await screen.findByRole("button", { name: "Suggest question alignments" });
    expect(
      screen.getByText(/up to 30 existing active or staged project questions/)
    ).toHaveTextContent("identifier, text, status, and type");
    expect(suggest).toBeDisabled();
    fireEvent.click(screen.getByLabelText(/I consent to send this checkpoint/));
    fireEvent.click(suggest);
    await waitFor(() => expect(aiBody).toEqual({ external_provider_acknowledged: true }));
    expect(screen.queryByText("Reviewed")).not.toBeInTheDocument();
    expect(await screen.findByRole("button", { name: "Submit each decision" })).toBeDisabled();
    expect(screen.queryByRole("button", { name: /Accept all/i })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Accept" }));
    await waitFor(() => expect(patchBody).toEqual({ payload: { text: "Does the effect persist?" }, status: "accepted" }));
    expect(screen.getByRole("button", { name: "Submit each decision" })).toBeEnabled();
  });

  it("serializes AI proposal decisions while a mutation is pending", async () => {
    let resolveDecision;
    let decisionCount = 0;
    const deferredDecision = new Promise((resolve) => {
      resolveDecision = resolve;
    });
    const readyDraft = {
      change_set_id: DRAFT_ID,
      operations: [{ entity_type: "question", op: "create", operation_id: OPERATION_ID, payload: { text: "Does the effect persist?" }, status: "proposed" }],
      project_id: PROJECT_ID,
      purpose: "member_checkpoint_alignment",
      status: "ready",
    };
    renderPage({
      initial: savedOnboarding({
        alignment: { draft: readyDraft, mode: "ai", question_resolutions: [], resolved_at: null },
        state: "alignment_ready",
      }),
      routes: [{
        match: `/graph-drafts/${DRAFT_ID}/operations/${OPERATION_ID}`,
        method: "PATCH",
        response: () => {
          decisionCount += 1;
          return deferredDecision;
        },
      }],
    });

    const accept = await screen.findByRole("button", { name: "Accept" });
    fireEvent.click(accept);
    fireEvent.click(accept);

    expect(decisionCount).toBe(1);
    expect(accept).toBeDisabled();
    expect(screen.getByRole("button", { name: "Reject" })).toBeDisabled();

    resolveDecision(apiResponse({
      ...readyDraft,
      operations: [{ ...readyDraft.operations[0], status: "accepted" }],
    }));
    await waitFor(() => expect(accept).toBeEnabled());
  });

  it("shows an owner's review note when changes are requested", async () => {
    renderPage({
      initial: savedOnboarding({
        alignment: {
          draft: {
            change_set_id: DRAFT_ID,
            operations: [{ entity_type: "question", op: "create", operation_id: OPERATION_ID, payload: { text: "Does the effect persist?" }, status: "accepted" }],
            project_id: PROJECT_ID,
            purpose: "member_checkpoint_alignment",
            review_note: "Use the shared control-cohort question instead of creating a duplicate.",
            status: "changes_requested",
          },
          mode: "ai",
          question_resolutions: [],
          resolved_at: null,
        },
        state: "changes_requested",
      }),
    });

    expect(await screen.findByText("Project owner feedback")).toBeInTheDocument();
    expect(screen.getByText(/shared control-cohort question/)).toBeInTheDocument();
  });

  it("reports terminal AI generation failure as a manual fallback", async () => {
    const setFlash = vi.fn();
    renderPage({
      initial: savedOnboarding(),
      routes: [{
        match: `${PATH}/ai-alignment`,
        method: "POST",
        response: apiResponse(savedOnboarding({
          alignment: {
            draft: {
              change_set_id: DRAFT_ID,
              operations: [],
              project_id: PROJECT_ID,
              purpose: "member_checkpoint_alignment",
              status: "failed",
            },
            mode: "ai",
            question_resolutions: [],
            resolved_at: null,
          },
          state: "checkpoint_ready",
        })),
      }],
      setFlash,
    });

    fireEvent.click(await screen.findByLabelText(/I consent to send this checkpoint/));
    fireEvent.click(screen.getByRole("button", { name: "Suggest question alignments" }));

    await waitFor(() => expect(setFlash).toHaveBeenLastCalledWith(
      "AI question alignment failed. Resolve the questions manually to continue."
    ));
    expect(screen.queryByText("Reviewed")).not.toBeInTheDocument();
    expect(await screen.findByRole("button", { name: "Save each resolution" })).toBeInTheDocument();
  });

  it("finishes an all-rejected AI alignment without claiming that an owner must review it", async () => {
    const rejectedDraft = {
      change_set_id: DRAFT_ID,
      operations: [{
        entity_type: "question",
        op: "create",
        operation_id: OPERATION_ID,
        payload: { text: "Does the effect persist?" },
        status: "rejected",
      }],
      project_id: PROJECT_ID,
      purpose: "member_checkpoint_alignment",
      status: "ready",
    };
    const setFlash = vi.fn();
    renderPage({
      initial: savedOnboarding({
        alignment: {
          draft: rejectedDraft,
          mode: "ai",
          question_resolutions: [],
          resolved_at: null,
        },
        state: "alignment_ready",
      }),
      routes: [{
        match: `/graph-drafts/${DRAFT_ID}/submit`,
        method: "POST",
        response: apiResponse({ ...rejectedDraft, status: "submitted" }),
      }],
      setFlash,
    });

    fireEvent.click(await screen.findByRole("button", { name: "Submit each decision" }));
    await waitFor(() => expect(setFlash).toHaveBeenLastCalledWith(
      "Question alignment complete. No shared graph changes were kept."
    ));
  });

  it("describes a member's all-rejected decision as checkpoint-only, not an owner rejection", async () => {
    renderPage({
      initial: savedOnboarding({
        alignment: {
          draft: {
            change_set_id: DRAFT_ID,
            context_packet: { member_onboarding_resolution: "checkpoint_only" },
            operations: [],
            project_id: PROJECT_ID,
            purpose: "member_checkpoint_alignment",
            status: "rejected",
          },
          mode: "ai",
          question_resolutions: [],
          resolved_at: "2026-08-13T12:30:00Z",
        },
        member_complete: true,
        state: "complete",
      }),
    });

    expect(
      await screen.findAllByText("Orientation complete — checkpoint only")
    ).toHaveLength(2);
    expect(
      screen.getByText(/You kept these live questions in your attributed checkpoint/)
    ).toBeInTheDocument();
    expect(screen.queryByText(/owner declined/i)).not.toBeInTheDocument();
  });

  it("shows an AI generation claim as resumable while it polls for readiness", async () => {
    renderPage({
      initial: savedOnboarding({
        alignment: {
          draft: {
            change_set_id: DRAFT_ID,
            operations: [],
            project_id: PROJECT_ID,
            purpose: "member_checkpoint_alignment",
            status: "drafting",
          },
          mode: "ai",
          question_resolutions: [],
          resolved_at: null,
        },
        state: "alignment_ready",
      }),
    });

    expect(await screen.findByText(/This page will update automatically, and you can safely return later/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Submit each decision" })).not.toBeInTheDocument();
  });

  it("renders source-labelled map items, copies the brief, and hands capture off with context", async () => {
    const clipboard = { writeText: vi.fn().mockResolvedValue(undefined) };
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: clipboard });
    const navigate = vi.fn();
    renderPage({
      navigate,
      initial: savedOnboarding({
        alignment: { draft: null, mode: "manual", question_resolutions: [{ action: "checkpoint_only", question_index: 0 }], resolved_at: "2026-08-13T12:30:00Z" },
        brief_markdown: "# Where the project stands\n\nNext: run the controls.",
        map_items: [
          { operation_id: null, question_id: SHARED_QUESTION_ID, question_index: 0, source: "shared", status: "active", text: "Which control is decisive?" },
          { operation_id: null, question_id: null, question_index: 1, source: "personal", status: "checkpoint_only", text: "Does the effect persist?" },
        ],
        state: "capture_pending",
      }),
    });

    const map = await screen.findByLabelText("Current-state question map");
    expect(within(map).getByText("Shared · active")).toBeInTheDocument();
    expect(within(map).getByText("Checkpoint only · checkpoint only")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Copy brief" }));
    await waitFor(() => expect(clipboard.writeText).toHaveBeenCalledWith("# Where the project stands\n\nNext: run the controls."));
    fireEvent.click(screen.getByRole("button", { name: "Make the first capture" }));
    expect(navigate).toHaveBeenCalledWith(
      `/app/capture?project_id=${PROJECT_ID}&checkpoint_note_id=${CHECKPOINT_ID}&return_to=${encodeURIComponent(`/app/projects/${PROJECT_ID}/onboarding`)}`
    );
  });
});

describe("OwnerOnboardingQueueBanner", () => {
  it("links every awaiting map to purpose-aware owner review and its return path", async () => {
    const navigate = vi.fn();
    installFetchMock([
      { match: `${PATH}/owner-queue`, response: apiResponse([{ accepted_operation_count: 2, checkpoint: { note_id: CHECKPOINT_ID }, draft: { change_set_id: DRAFT_ID, operations: [], project_id: PROJECT_ID, status: "submitted" }, member_user_id: "user-2", member_username: "trainee@example.org", project_id: PROJECT_ID }]) },
    ]);
    render(
      <OwnerOnboardingQueueBanner
        enabled
        projectId={PROJECT_ID}
        token="token-1"
        navigate={navigate}
        returnPath="/app/setup"
      />
    );
    expect(await screen.findByText("1 member map awaits your commit")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Review and commit" }));
    expect(navigate).toHaveBeenCalledWith(
      `/app/graph-drafts/${DRAFT_ID}?return_to=${encodeURIComponent("/app/setup")}`
    );
  });

  it("clears the previous project's queue while the next project loads", async () => {
    const projectB = "8a93f375-b2ad-48c3-bf8f-4f083ad5791b";
    let resolveProjectB;
    const deferredProjectB = new Promise((resolve) => {
      resolveProjectB = resolve;
    });
    installFetchMock([
      { match: `${PATH}/owner-queue`, response: apiResponse([{ accepted_operation_count: 1, checkpoint: { note_id: CHECKPOINT_ID }, draft: { change_set_id: DRAFT_ID, operations: [], project_id: PROJECT_ID, status: "submitted" }, member_user_id: "user-2", member_username: "trainee@example.org", project_id: PROJECT_ID }]) },
      { match: `/projects/${projectB}/member-onboarding/owner-queue`, response: () => deferredProjectB },
    ]);
    const { rerender } = render(
      <OwnerOnboardingQueueBanner projectId={PROJECT_ID} token="token-1" navigate={vi.fn()} />
    );
    expect(await screen.findByText("1 member map awaits your commit")).toBeInTheDocument();

    rerender(
      <OwnerOnboardingQueueBanner projectId={projectB} token="token-1" navigate={vi.fn()} />
    );
    expect(screen.queryByText("1 member map awaits your commit")).not.toBeInTheDocument();

    await act(async () => {
      resolveProjectB(apiResponse([]));
      await deferredProjectB;
    });
  });
});

describe("onboarding capture handoff", () => {
  it("locks the checkpoint note target, keeps normal context, and returns without reserved metadata", async () => {
    const returnPath = `/app/projects/${PROJECT_ID}/onboarding`;
    window.history.replaceState(
      {},
      "",
      `/app/capture?project_id=${PROJECT_ID}&checkpoint_note_id=${CHECKPOINT_ID}&return_to=${encodeURIComponent(returnPath)}`
    );
    const navigate = vi.fn();
    let noteBody;
    installFetchMock([
      { match: buildApiPath("/graph-drafts", { project_id: PROJECT_ID, limit: 10 }), response: apiResponse([]) },
      { match: buildApiPath("/notes", { project_id: PROJECT_ID, limit: 10 }), response: apiResponse([]) },
      { match: buildApiPath("/analyses", { project_id: PROJECT_ID, limit: 50 }), response: apiResponse([]) },
      { match: buildApiPath("/claims", { project_id: PROJECT_ID, limit: 50 }), response: apiResponse([]) },
      {
        match: "/notes",
        method: "POST",
        response: (request) => {
          noteBody = JSON.parse(request.init.body);
          return apiResponse({ note_id: "bda40977-8ed1-43cf-a063-1e7995304126" }, 201);
        },
      },
    ]);

    render(
      <MobileCaptureCard
        token="token-1"
        ownerId="user-1"
        canWrite
        projects={[{ name: "Ongoing odor project", project_id: PROJECT_ID }]}
        selectedProjectId={PROJECT_ID}
        onSelectedProjectChange={vi.fn()}
        questions={[{ question_id: SHARED_QUESTION_ID, status: "active", text: "Which control is decisive?" }]}
        datasets={[]}
        sessions={[]}
        navigate={navigate}
        setBusy={vi.fn()}
        setFlash={vi.fn()}
        refreshProjectCounts={vi.fn().mockResolvedValue(undefined)}
        refreshRecentNotes={vi.fn().mockResolvedValue(undefined)}
      />
    );

    expect(await screen.findByText("Tracking checkpoint attached")).toBeInTheDocument();
    expect(screen.getByLabelText("Project")).toBeDisabled();
    fireEvent.change(screen.getByLabelText("Active question (optional)"), { target: { value: SHARED_QUESTION_ID } });
    fireEvent.change(screen.getByLabelText("Message or hint"), { target: { value: "Control cohort scheduled" } });
    fireEvent.click(screen.getByRole("button", { name: "Save capture" }));

    await waitFor(() => expect(noteBody).toBeTruthy());
    expect(noteBody.targets).toEqual([
      { entity_id: SHARED_QUESTION_ID, entity_type: "question" },
      { entity_id: CHECKPOINT_ID, entity_type: "note" },
    ]);
    expect(Object.keys(noteBody.metadata).some((key) => key.startsWith("member_onboarding_"))).toBe(false);
    await waitFor(() => expect(navigate).toHaveBeenCalledWith(returnPath));
  });
});
