import * as React from "react";

import { useMemberOnboarding } from "../hooks/useMemberOnboarding.js";
import { apiRequest } from "../shared/api.js";
import { formatDate } from "../shared/formatters.js";
import { memberOnboarding as memberOnboardingGateway } from "../shared/gateways/index.js";
import { RequestEditAccess } from "../shared/ui.jsx";

const { useEffect, useMemo, useRef, useState } = React;

const EMPTY_FIELDS = {
  asOf: "",
  currentOutput: "",
  liveQuestions: [""],
  nextMove: "",
  sourceText: "",
  strongestContext: "",
};

function checkpointId(onboarding) {
  return onboarding?.checkpoint?.note_id || onboarding?.checkpoint?.checkpoint_note_id || "";
}

function humanStateLabel(state, onboarding) {
  const draft = onboarding?.alignment?.draft;
  const draftStatus = draft?.status;
  const memberKeptCheckpointOnly =
    draft?.context_packet?.member_onboarding_resolution === "checkpoint_only";
  if (onboarding?.member_complete && onboarding?.owner_commit_pending) {
    return "Complete — awaiting project owner";
  }
  if (onboarding?.member_complete && draftStatus === "rejected") {
    return memberKeptCheckpointOnly
      ? "Orientation complete — checkpoint only"
      : "Complete — shared changes declined";
  }
  if (onboarding?.member_complete && draftStatus === "committed") {
    return "Orientation complete — shared map committed";
  }
  if (onboarding?.member_complete) {
    return "Orientation complete";
  }
  const labels = {
    alignment_ready: "Question review pending",
    awaiting_owner: "Awaiting project owner",
    capture_pending: "First capture pending",
    checkpoint_ready: "Question alignment pending",
    changes_requested: "Owner requested changes",
    committed: "Shared map committed",
    complete: "Orientation complete",
    not_started: "Checkpoint pending",
    rejected: memberKeptCheckpointOnly
      ? "Question alignment complete — checkpoint only"
      : "Owner declined shared changes",
  };
  return labels[state] || String(state || "In progress").replaceAll("_", " ");
}

function statusClass(state) {
  if (["complete", "committed"].includes(state)) {
    return "pill review-approved";
  }
  if (["changes_requested", "failed", "rejected"].includes(state)) {
    return "pill review-rejected";
  }
  return "pill review-pending";
}

function operationText(operation, questions) {
  const payload = operation?.payload || {};
  const targetId =
    payload.question_id ||
    payload.target_question_id ||
    payload.entity_id ||
    payload.target_id ||
    payload.targets?.find((target) => target?.entity_type === "question")?.entity_id;
  const target = questions.find((question) => question.question_id === targetId);
  return (
    payload.text ||
    payload.question_text ||
    payload.target_question_text ||
    target?.text ||
    operation?.summary ||
    "Question alignment proposal"
  );
}

function operationLiveQuestionIndex(operation, fallback) {
  const match = String(operation?.client_ref || "").match(/^live_question_([0-2])$/);
  return match ? Number(match[1]) : fallback;
}

function operationQuestionTarget(operation) {
  return String(
    operation?.payload?.targets?.find((target) => target?.entity_type === "question")?.entity_id || ""
  );
}

function mapItemText(item) {
  return item?.text || item?.label || item?.question_text || item?.title || "Question";
}

function mapItemState(item) {
  const raw = item?.source || "shared";
  const labels = {
    committed: "Shared",
    member_reviewed: "Awaiting owner",
    pending:
      item?.status === "proposed" ? "Awaiting your review" : "Awaiting owner",
    personal: "Checkpoint only",
    proposed: "AI proposal",
    shared: "Shared",
    staged: "Shared staged",
    unreviewed: "Awaiting your review",
    awaiting_owner: "Awaiting owner",
  };
  const sourceLabel = labels[raw] || String(raw).replaceAll("_", " ");
  const recordStatus = String(item?.status || "").replaceAll("_", " ");
  return recordStatus && recordStatus !== raw ? `${sourceLabel} · ${recordStatus}` : sourceLabel;
}

function CheckpointForm({ disabled, onSave }) {
  const [fields, setFields] = useState(EMPTY_FIELDS);
  const [validationError, setValidationError] = useState("");
  const enteredLength = useMemo(
    () =>
      [
        fields.currentOutput,
        ...fields.liveQuestions,
        fields.strongestContext,
        fields.nextMove,
        fields.sourceText,
      ].join("\n\n").length,
    [fields]
  );

  function setQuestion(index, value) {
    setFields((current) => ({
      ...current,
      liveQuestions: current.liveQuestions.map((question, questionIndex) =>
        questionIndex === index ? value : question
      ),
    }));
  }

  async function submit(event) {
    event.preventDefault();
    const liveQuestions = fields.liveQuestions.map((value) => value.trim()).filter(Boolean);
    const normalized = liveQuestions.map((value) => value.toLocaleLowerCase());
    if (
      !fields.currentOutput.trim() ||
      !fields.strongestContext.trim() ||
      !fields.nextMove.trim() ||
      liveQuestions.length === 0
    ) {
      setValidationError("Complete all four checkpoint prompts before saving.");
      return;
    }
    if (new Set(normalized).size !== normalized.length) {
      setValidationError("Each live question must be unique.");
      return;
    }
    setValidationError("");
    try {
      await onSave({
        as_of: fields.asOf ? new Date(fields.asOf).toISOString() : null,
        current_output_or_decision: fields.currentOutput.trim(),
        live_questions: liveQuestions,
        next_move: fields.nextMove.trim(),
        source_text: fields.sourceText.trim() || null,
        strongest_recent_context: fields.strongestContext.trim(),
      });
    } catch {
      // The shared flash surface displays the server's reason.
    }
  }

  return (
    <form className="form member-onboarding-checkpoint" onSubmit={submit}>
      <label>
        What output or decision are you working toward now?
        <textarea
          disabled={disabled}
          onChange={(event) => setFields((current) => ({ ...current, currentOutput: event.target.value }))}
          placeholder="The result, figure, experiment, paper section, or decision currently in motion"
          value={fields.currentOutput}
        />
      </label>
      <fieldset>
        <legend>What live questions are guiding the work?</legend>
        <p className="subtle">Add one to three. You will decide how each maps to the shared project.</p>
        {fields.liveQuestions.map((question, index) => (
          <div className="inline member-onboarding-question-input" key={index}>
            <label>
              Question {index + 1}
              <input
                disabled={disabled}
                onChange={(event) => setQuestion(index, event.target.value)}
                value={question}
              />
            </label>
            {fields.liveQuestions.length > 1 ? (
              <button
                className="btn-secondary"
                disabled={disabled}
                onClick={() =>
                  setFields((current) => ({
                    ...current,
                    liveQuestions: current.liveQuestions.filter((_, itemIndex) => itemIndex !== index),
                  }))
                }
                type="button"
              >
                Remove
              </button>
            ) : null}
          </div>
        ))}
        {fields.liveQuestions.length < 3 ? (
          <button
            className="btn-secondary"
            disabled={disabled}
            onClick={() =>
              setFields((current) => ({
                ...current,
                liveQuestions: [...current.liveQuestions, ""],
              }))
            }
            type="button"
          >
            Add another question
          </button>
        ) : null}
      </fieldset>
      <label>
        What recent result or context matters most?
        <textarea
          disabled={disabled}
          onChange={(event) => setFields((current) => ({ ...current, strongestContext: event.target.value }))}
          placeholder="The observation, constraint, or result someone needs to understand the current state"
          value={fields.strongestContext}
        />
      </label>
      <label>
        What is the next move?
        <textarea
          disabled={disabled}
          onChange={(event) => setFields((current) => ({ ...current, nextMove: event.target.value }))}
          placeholder="The next experiment, analysis, decision, or conversation"
          value={fields.nextMove}
        />
      </label>
      <label>
        State as of (optional)
        <input
          disabled={disabled}
          onChange={(event) => setFields((current) => ({ ...current, asOf: event.target.value }))}
          type="datetime-local"
          value={fields.asOf}
        />
      </label>
      <label>
        Paste a project brief, aims, or meeting context (optional)
        <textarea
          disabled={disabled}
          onChange={(event) => setFields((current) => ({ ...current, sourceText: event.target.value }))}
          rows={8}
          value={fields.sourceText}
        />
      </label>
      <p className="subtle">
        {enteredLength.toLocaleString()} entered characters. The server verifies that the complete rendered checkpoint, including its headings and tracking boundary, is 64,000 characters or fewer; it never truncates your text.
      </p>
      {validationError ? <p className="flash error">{validationError}</p> : null}
      <button className="btn-primary" disabled={disabled}>
        Save tracking checkpoint
      </button>
    </form>
  );
}

function SavedCheckpoint({ fields, checkpoint }) {
  const questions = fields?.live_questions || [];
  return (
    <div className="stack member-onboarding-saved-checkpoint">
      <p><strong>Working toward</strong><br />{fields?.current_output_or_decision}</p>
      <div>
        <strong>Live questions</strong>
        <ol className="compact-list">
          {questions.map((question) => <li key={question}>{question}</li>)}
        </ol>
      </div>
      <p><strong>Strongest recent context</strong><br />{fields?.strongest_recent_context}</p>
      <p><strong>Next move</strong><br />{fields?.next_move}</p>
      <p className="subtle">
        Saved {checkpoint?.created_at ? formatDate(checkpoint.created_at) : "as your personal project checkpoint"}.
        {fields?.source_text_present ? " Optional source context is attached." : ""}
      </p>
    </div>
  );
}

function ManualAlignment({ questions, sharedQuestions, disabled, onSave }) {
  const [resolutions, setResolutions] = useState(() =>
    questions.map((_, index) => ({ action: "", question_index: index }))
  );
  const [validationError, setValidationError] = useState("");

  useEffect(() => {
    setResolutions(questions.map((_, index) => ({ action: "", question_index: index })));
  }, [questions]);

  function patch(index, patch) {
    setResolutions((current) =>
      current.map((resolution) =>
        resolution.question_index === index ? { ...resolution, ...patch } : resolution
      )
    );
  }

  async function submit(event) {
    event.preventDefault();
    if (resolutions.some((resolution) => !resolution.action)) {
      setValidationError("Choose one resolution for every live question.");
      return;
    }
    if (
      resolutions.some(
        (resolution) => resolution.action === "link_existing" && !resolution.question_id
      )
    ) {
      setValidationError("Choose the shared question for every link resolution.");
      return;
    }
    setValidationError("");
    try {
      await onSave(
        resolutions.map((resolution) => ({
          action: resolution.action,
          existing_question_id:
            resolution.action === "link_existing" ? resolution.question_id : undefined,
          question_index: resolution.question_index,
        }))
      );
    } catch {
      // Shared flash message already contains the server error.
    }
  }

  return (
    <form className="form" onSubmit={submit}>
      <div className="stack">
        {questions.map((question, index) => {
          const resolution = resolutions[index] || {};
          return (
            <section className="item" key={`${index}-${question}`}>
              <strong>{question}</strong>
              <label>
                Resolution
                <select
                  aria-label={`Resolution for question ${index + 1}`}
                  disabled={disabled}
                  onChange={(event) => patch(index, { action: event.target.value, question_id: "" })}
                  value={resolution.action || ""}
                >
                  <option value="">Choose one</option>
                  <option value="link_existing">Link to a shared question</option>
                  <option value="create_staged">Add this exact text as a staged question</option>
                  <option value="checkpoint_only">Keep only in my checkpoint</option>
                </select>
              </label>
              {resolution.action === "link_existing" ? (
                <label>
                  Shared question
                  <select
                    aria-label={`Shared question for question ${index + 1}`}
                    disabled={disabled}
                    onChange={(event) => patch(index, { question_id: event.target.value })}
                    value={resolution.question_id || ""}
                  >
                    <option value="">Choose an active or staged question</option>
                    {sharedQuestions.map((item) => (
                      <option key={item.question_id} value={item.question_id}>{item.text}</option>
                    ))}
                  </select>
                </label>
              ) : null}
            </section>
          );
        })}
      </div>
      {validationError ? <p className="flash error">{validationError}</p> : null}
      <button className="btn-primary" disabled={disabled}>Save each resolution</button>
    </form>
  );
}

function AiAlignmentReview({ draft: initialDraft, liveQuestions, questions, token, disabled, onReload, setBusy, setFlash }) {
  const [draft, setDraft] = useState(initialDraft);
  const [editedText, setEditedText] = useState({});
  const [editedTarget, setEditedTarget] = useState({});
  const [pendingCommand, setPendingCommand] = useState("");
  const changeSetId = initialDraft?.change_set_id;
  const activeDraftIdRef = useRef(changeSetId);
  const pendingCommandRef = useRef("");

  useEffect(() => {
    activeDraftIdRef.current = changeSetId;
    pendingCommandRef.current = "";
    setPendingCommand("");
    setEditedText({});
    setEditedTarget({});
  }, [changeSetId]);

  useEffect(() => {
    let canceled = false;
    setDraft(initialDraft);
    if (!changeSetId || Array.isArray(initialDraft?.operations)) {
      return () => { canceled = true; };
    }
    apiRequest(`/graph-drafts/${changeSetId}`, { token })
      .then((loaded) => { if (!canceled) setDraft(loaded); })
      .catch(() => {});
    return () => { canceled = true; };
  }, [changeSetId, initialDraft, token]);

  const operations = draft?.operations || [];
  const editable = !disabled && ["ready", "changes_requested"].includes(draft?.status);
  const resolved = operations.length > 0 && operations.every((operation) =>
    ["accepted", "rejected"].includes(operation.status)
  );

  async function decide(operation, status) {
    if (!editable || pendingCommandRef.current) {
      return;
    }
    const requestDraftId = draft.change_set_id;
    const commandKey = `operation:${operation.operation_id}`;
    const payload = { ...(operation.payload || {}) };
    if (Object.prototype.hasOwnProperty.call(payload, "text") && editedText[operation.operation_id] !== undefined) {
      payload.text = editedText[operation.operation_id].trim();
    }
    if (Array.isArray(payload.targets) && editedTarget[operation.operation_id]) {
      payload.targets = [{
        entity_id: editedTarget[operation.operation_id],
        entity_type: "question",
      }];
    }
    pendingCommandRef.current = commandKey;
    setPendingCommand(commandKey);
    setBusy(true);
    setFlash("", "");
    try {
      const next = await apiRequest(
        `/graph-drafts/${requestDraftId}/operations/${operation.operation_id}`,
        { body: { payload, status }, method: "PATCH", token }
      );
      if (activeDraftIdRef.current === requestDraftId) {
        setDraft(next);
        setFlash(status === "accepted" ? "Proposal accepted." : "Proposal rejected.");
      }
    } catch (err) {
      if (activeDraftIdRef.current === requestDraftId) {
        setFlash("", err.message || "The proposal decision could not be saved.");
      }
    } finally {
      if (pendingCommandRef.current === commandKey) {
        pendingCommandRef.current = "";
        setPendingCommand("");
      }
      setBusy(false);
    }
  }

  async function submit() {
    if (!editable || !resolved || pendingCommandRef.current) {
      return;
    }
    const requestDraftId = draft.change_set_id;
    const commandKey = "submit";
    const acceptedCount = operations.filter((operation) => operation.status === "accepted").length;
    pendingCommandRef.current = commandKey;
    setPendingCommand(commandKey);
    setBusy(true);
    setFlash("", "");
    try {
      await apiRequest(`/graph-drafts/${requestDraftId}/submit`, { method: "POST", token });
      if (activeDraftIdRef.current === requestDraftId) {
        await onReload();
        setFlash(
          acceptedCount > 0
            ? "Question alignment submitted to the project owner."
            : "Question alignment complete. No shared graph changes were kept."
        );
      }
    } catch (err) {
      if (activeDraftIdRef.current === requestDraftId) {
        setFlash("", err.message || "Question alignment could not be submitted.");
      }
    } finally {
      if (pendingCommandRef.current === commandKey) {
        pendingCommandRef.current = "";
        setPendingCommand("");
      }
      setBusy(false);
    }
  }

  if (!draft) {
    return <p className="subtle">Preparing the question proposals…</p>;
  }
  if (draft.status === "drafting") {
    return (
      <p className="flash warning" role="status">
        AI question alignment is being prepared. This page will update automatically, and you can safely return later.
      </p>
    );
  }
  if (draft.status === "failed") {
    return <p className="flash error">AI alignment failed. Use the manual path below to continue.</p>;
  }
  return (
    <div className="stack member-onboarding-ai-review">
      {draft.review_note && ["changes_requested", "rejected"].includes(draft.status) ? (
        <div className="flash warning" role="status">
          <strong>{draft.status === "changes_requested" ? "Project owner feedback" : "Project owner note"}</strong>
          <p>{draft.review_note}</p>
        </div>
      ) : null}
      {operations.map((operation, index) => {
        const hasEditableText = Object.prototype.hasOwnProperty.call(operation.payload || {}, "text");
        const hasEditableTarget = Array.isArray(operation.payload?.targets);
        const liveQuestionIndex = operationLiveQuestionIndex(operation, index);
        return (
          <section className="item" key={operation.operation_id}>
            <div className="item-head">
              <strong>Proposal {index + 1}</strong>
              <span className={statusClass(operation.status)}>{operation.status}</span>
            </div>
            {liveQuestions[liveQuestionIndex] ? (
              <p><strong>Your live question:</strong> {liveQuestions[liveQuestionIndex]}</p>
            ) : null}
            {hasEditableText ? (
              <label>
                Proposed staged question
                <textarea
                  disabled={!editable}
                  onChange={(event) => setEditedText((current) => ({
                    ...current,
                    [operation.operation_id]: event.target.value,
                  }))}
                  value={editedText[operation.operation_id] ?? operationText(operation, questions)}
                />
              </label>
            ) : hasEditableTarget ? (
              <label>
                Proposed shared question
                <select
                  disabled={!editable}
                  onChange={(event) => setEditedTarget((current) => ({
                    ...current,
                    [operation.operation_id]: event.target.value,
                  }))}
                  value={editedTarget[operation.operation_id] ?? operationQuestionTarget(operation)}
                >
                  {questions.map((question) => (
                    <option key={question.question_id} value={question.question_id}>{question.text}</option>
                  ))}
                </select>
              </label>
            ) : <p>{operationText(operation, questions)}</p>}
            {operation.rationale ? <p className="subtle">Model inference: {operation.rationale}</p> : null}
            <div className="inline">
              <button className="btn-primary" disabled={!editable || Boolean(pendingCommand)} onClick={() => decide(operation, "accepted")} type="button">Accept</button>
              <button className="btn-danger" disabled={!editable || Boolean(pendingCommand)} onClick={() => decide(operation, "rejected")} type="button">Reject</button>
            </div>
          </section>
        );
      })}
      {operations.length === 0 ? <p className="subtle">No graph changes were proposed.</p> : null}
      {editable ? (
        <button className="btn-primary" disabled={Boolean(pendingCommand) || (operations.length > 0 && !resolved)} onClick={submit} type="button">
          {pendingCommand === "submit"
            ? "Submitting…"
            : draft.status === "changes_requested"
              ? "Resubmit each decision"
              : "Submit each decision"}
        </button>
      ) : null}
      {editable && operations.length > 0 && !resolved ? (
        <p className="subtle">Accept or reject every proposal before submitting. There is no bulk accept in orientation.</p>
      ) : null}
    </div>
  );
}

function ProjectStatePayoff({ onboarding, onCopy }) {
  const items = onboarding.map_items || [];
  if (items.length === 0 && !onboarding.brief_markdown) {
    return <p className="subtle">Your map and brief will appear after question alignment.</p>;
  }
  return (
    <div className="member-onboarding-payoff">
      <section>
        <h4>Current-state map</h4>
        <div className="stack member-onboarding-map" aria-label="Current-state question map">
          {items.map((item, index) => (
            <article className={`item map-state-${item.source || "shared"} map-status-${item.status || "unknown"}`} key={item.item_id || item.question_id || index}>
              <strong>{mapItemText(item)}</strong>
              <span className="pill">{mapItemState(item)}</span>
            </article>
          ))}
        </div>
        <div className="inline member-onboarding-map-legend" aria-label="Map legend">
          <span className="pill">Solid: shared</span>
          <span className="pill">Outlined: awaiting owner</span>
          <span className="pill">Muted: unreviewed</span>
          <span className="pill">Personal: checkpoint only</span>
        </div>
      </section>
      {onboarding.brief_markdown ? (
        <section>
          <div className="item-head">
            <h4>Project-now brief</h4>
            <button className="btn-secondary" onClick={() => onCopy(onboarding.brief_markdown)} type="button">Copy brief</button>
          </div>
          <pre className="command-block member-onboarding-brief"><code>{onboarding.brief_markdown}</code></pre>
        </section>
      ) : null}
    </div>
  );
}

function MemberOnboardingPage({
  token,
  project,
  projectId,
  projectAccess,
  questions,
  navigate,
  setBusy,
  setFlash,
}) {
  const workflow = useMemberOnboarding({ projectId, token, setBusy, setFlash });
  const reloadOnboarding = workflow.load;
  const [manualOpen, setManualOpen] = useState(false);
  const [providerConsent, setProviderConsent] = useState(false);
  const [aiRequestPending, setAiRequestPending] = useState(false);
  const aiRequestPendingRef = useRef(false);
  const onboarding = workflow.onboarding;
  const capabilities = onboarding?.capabilities || {};
  const canCreateCheckpoint = Boolean(capabilities.can_create_checkpoint ?? projectAccess?.canContribute);
  const canAlign = Boolean(capabilities.can_align ?? projectAccess?.canContribute);
  const canCapture = Boolean(capabilities.can_capture ?? projectAccess?.canContribute);
  const savedCheckpoint = onboarding?.checkpoint || null;
  const guidedFields = onboarding?.guided_fields || {};
  const alignment = onboarding?.alignment || {};
  const draft = alignment?.draft || null;
  const memberKeptCheckpointOnly =
    draft?.context_packet?.member_onboarding_resolution === "checkpoint_only";
  const liveQuestions = guidedFields.live_questions || [];
  const sharedQuestions = questions.filter((question) => ["active", "staged"].includes(question.status));
  const hasResolvedAlignment = Boolean(
    (alignment.mode === "manual" && alignment.resolved_at) ||
      ["submitted", "committed", "rejected"].includes(draft?.status)
  );
  const state = onboarding?.state || "";

  useEffect(() => {
    aiRequestPendingRef.current = false;
    setAiRequestPending(false);
    setManualOpen(false);
    setProviderConsent(false);
  }, [projectId, savedCheckpoint?.note_id]);

  useEffect(() => {
    if (draft?.status === "failed") {
      setManualOpen(true);
    }
  }, [draft?.status]);

  useEffect(() => {
    if (draft?.status !== "drafting") {
      return undefined;
    }
    const timer = window.setInterval(() => {
      reloadOnboarding();
    }, 2000);
    return () => window.clearInterval(timer);
  }, [draft?.status, reloadOnboarding]);

  async function copyBrief(text) {
    try {
      await navigator.clipboard.writeText(text);
      setFlash("Project-now brief copied.");
    } catch {
      setFlash("", "Copy failed — select the brief and copy it manually.");
    }
  }

  async function requestAiAlignment() {
    if (!providerConsent || aiRequestPendingRef.current) {
      return;
    }
    aiRequestPendingRef.current = true;
    setAiRequestPending(true);
    const externalProviderAcknowledged = providerConsent;
    setProviderConsent(false);
    try {
      await workflow.startAiAlignment(externalProviderAcknowledged);
    } catch {
      // The shared flash surface displays the server's reason.
    } finally {
      aiRequestPendingRef.current = false;
      setAiRequestPending(false);
    }
  }

  if (workflow.loading && !onboarding) {
    return <article className="card span-12"><h2>Orient to this project</h2><p className="subtle">Loading your project checkpoint…</p></article>;
  }
  if (workflow.error && !onboarding) {
    return <article className="card span-12"><h2>Orient to this project</h2><p className="flash error">{workflow.error}</p><button className="btn-secondary" onClick={reloadOnboarding} type="button">Try again</button></article>;
  }

  return (
    <article className="card span-12 member-onboarding-page">
      <div className="item-head">
        <div>
          <p className="eyebrow">Ongoing-project orientation</p>
          <h2>Start tracking {project?.name || "this project"} from where it is now</h2>
          <p className="subtle">Capture the live scientific thread without reconstructing the full history.</p>
        </div>
        <div className="stack member-onboarding-heading-actions">
          <span className={statusClass(state)}>{humanStateLabel(state, onboarding)}</span>
          <button className="btn-secondary" onClick={() => navigate("/app")} type="button">Workspace</button>
        </div>
      </div>

      <p className="flash warning" role="status">
        Your checkpoint is attributed to you and visible to project members. Routine Lab Tracker coverage begins here; earlier project coverage is selective.
      </p>

      {!canCreateCheckpoint && !savedCheckpoint ? (
        <section className="card-inset">
          <h3>Read-only orientation</h3>
          <p>You can inspect shared project context, but a contributor or owner role is required to save a checkpoint, align questions, or make the first capture.</p>
          <RequestEditAccess selectedProject={project} />
        </section>
      ) : null}

      <ol className="setup-steps member-onboarding-steps">
        <li className="card-inset setup-step">
          <div className="item-head"><div><p className="eyebrow">Step 1</p><h3>Your tracking checkpoint</h3></div><span className="pill">{savedCheckpoint ? "Saved" : "Required"}</span></div>
          {savedCheckpoint ? <SavedCheckpoint fields={guidedFields} checkpoint={savedCheckpoint} /> : <CheckpointForm disabled={!canCreateCheckpoint} onSave={workflow.saveCheckpoint} />}
        </li>

        <li className="card-inset setup-step">
          <div className="item-head"><div><p className="eyebrow">Step 2</p><h3>Place your live questions</h3></div><span className="pill">{hasResolvedAlignment || ["submitted", "committed", "rejected"].includes(draft?.status) ? "Reviewed" : "Required"}</span></div>
          {!savedCheckpoint ? <p className="subtle">Save your checkpoint first.</p> : hasResolvedAlignment && !draft ? (
            <p className="flash ok">You resolved every live question manually.</p>
          ) : draft ? (
            <>
              <AiAlignmentReview draft={draft} liveQuestions={liveQuestions} questions={sharedQuestions} token={token} disabled={!canAlign} onReload={reloadOnboarding} setBusy={setBusy} setFlash={setFlash} />
              {draft.status === "failed" ? <ManualAlignment questions={liveQuestions} sharedQuestions={sharedQuestions} disabled={!canAlign} onSave={workflow.saveManualAlignment} /> : null}
            </>
          ) : (
            <div className="stack">
              <section className="item">
                <h4>Ask AI to suggest alignments</h4>
                <p className="subtle">The complete checkpoint and up to 30 existing active or staged project questions will be sent to the deployment’s configured external AI provider. For each candidate question, Lab Tracker sends its identifier, text, status, and type. Nothing enters the shared graph automatically.</p>
                <label className="check-row">
                  <input checked={providerConsent} disabled={!canAlign || aiRequestPending} onChange={(event) => setProviderConsent(event.target.checked)} type="checkbox" />
                  I consent to send this checkpoint and those candidate-question fields to the configured external AI provider for this request.
                </label>
                <button className="btn-primary" disabled={!canAlign || !providerConsent || aiRequestPending} onClick={requestAiAlignment} type="button">{aiRequestPending ? "Requesting suggestions…" : "Suggest question alignments"}</button>
              </section>
              <button className="btn-secondary" disabled={!canAlign} onClick={() => setManualOpen((current) => !current)} type="button">{manualOpen ? "Hide manual alignment" : "Align questions manually"}</button>
              {manualOpen ? <ManualAlignment questions={liveQuestions} sharedQuestions={sharedQuestions} disabled={!canAlign} onSave={workflow.saveManualAlignment} /> : null}
            </div>
          )}
        </li>

        <li className="card-inset setup-step">
          <div className="item-head"><div><p className="eyebrow">Step 3</p><h3>Your project-now map and brief</h3></div><span className="pill">Current state</span></div>
          {onboarding ? <ProjectStatePayoff onboarding={onboarding} onCopy={copyBrief} /> : null}
          {onboarding?.owner_commit_pending ? <p className="flash warning">Your reviewed map is useful now and is waiting for a project owner before it becomes shared project structure.</p> : null}
          {state === "changes_requested" || draft?.status === "changes_requested" ? <p className="flash warning">A project owner requested changes. Review each AI proposal above, then resubmit.</p> : null}
          {state === "rejected" || draft?.status === "rejected" ? (
            <p className="subtle">
              {memberKeptCheckpointOnly
                ? "You kept these live questions in your attributed checkpoint without proposing shared graph changes."
                : "The owner declined the proposed shared changes. Your attributed checkpoint remains part of the project record."}
            </p>
          ) : null}
        </li>

        <li className="card-inset setup-step">
          <div className="item-head"><div><p className="eyebrow">Step 4</p><h3>Capture what happens next</h3></div><span className="pill">{onboarding?.first_capture ? "Captured" : "Required"}</span></div>
          {onboarding?.first_capture ? (
            <p className="flash ok">First forward capture saved {onboarding.first_capture.created_at ? formatDate(onboarding.first_capture.created_at) : ""}. This checkpoint is now connected to ongoing work.</p>
          ) : (
            <>
              <p>Make one genuine text, photo, or voice capture. The checkpoint target stays attached while you can add ordinary question, session, dataset, analysis, or claim context.</p>
              <button
                className="btn-primary"
                disabled={!canCapture || !checkpointId(onboarding)}
                onClick={() => {
                  const returnPath = `/app/projects/${projectId}/onboarding`;
                  navigate(`/app/capture?project_id=${encodeURIComponent(projectId)}&checkpoint_note_id=${encodeURIComponent(checkpointId(onboarding))}&return_to=${encodeURIComponent(returnPath)}`);
                }}
                type="button"
              >
                Make the first capture
              </button>
              {!canCapture ? <RequestEditAccess selectedProject={project} /> : null}
            </>
          )}
        </li>
      </ol>

      <div className="row-between setup-finish">
        <p className="subtle">{onboarding?.member_complete ? humanStateLabel(state, onboarding) : "Finish the checkpoint, question decisions, and one forward capture."}</p>
        <button className="btn-primary" disabled={!onboarding?.member_complete} onClick={() => navigate("/app")} type="button">Finish in workspace</button>
      </div>
    </article>
  );
}

function OwnerOnboardingQueueBanner({
  projectId,
  token,
  navigate,
  returnPath = "/app",
}) {
  const currentContextRef = useRef({ projectId, token });
  const [queueResource, setQueueResource] = useState({ data: [], projectId: "", token: "" });
  const queue =
    queueResource.projectId === projectId && queueResource.token === token
      ? queueResource.data
      : [];

  useEffect(() => {
    let canceled = false;
    currentContextRef.current = { projectId, token };
    setQueueResource({ data: [], projectId, token });
    if (!projectId) {
      return () => { canceled = true; };
    }
    memberOnboardingGateway
      .listOwnerQueue(projectId, { token })
      .then(({ data }) => {
        if (
          !canceled &&
          currentContextRef.current.projectId === projectId &&
          currentContextRef.current.token === token
        ) {
          setQueueResource({ data: data || [], projectId, token });
        }
      })
      .catch(() => {
        if (
          !canceled &&
          currentContextRef.current.projectId === projectId &&
          currentContextRef.current.token === token
        ) {
          setQueueResource({ data: [], projectId, token });
        }
      });
    return () => { canceled = true; };
  }, [projectId, token]);

  if (queue.length === 0) {
    return null;
  }
  return (
    <aside className="flash warning span-12 member-onboarding-owner-queue" role="status">
      <div className="row-between">
        <strong>{queue.length === 1 ? "1 member map awaits your commit" : `${queue.length} member maps await your commit`}</strong>
        <span className="pill">Project owner</span>
      </div>
      <div className="stack">
        {queue.map((item) => {
          const draft = item.draft || {};
          return (
            <div className="row-between" key={draft.change_set_id || item.member_user_id}>
              <span>
                {item.member_username || "Project member"} · {item.accepted_operation_count || 0} accepted {item.accepted_operation_count === 1 ? "change" : "changes"}
              </span>
              {draft.change_set_id ? (
                <button
                  className="btn-secondary"
                  onClick={() => {
                    navigate(`/app/graph-drafts/${draft.change_set_id}?return_to=${encodeURIComponent(returnPath)}`);
                  }}
                  type="button"
                >
                  Review and commit
                </button>
              ) : null}
            </div>
          );
        })}
      </div>
    </aside>
  );
}

export { MemberOnboardingPage, OwnerOnboardingQueueBanner };
