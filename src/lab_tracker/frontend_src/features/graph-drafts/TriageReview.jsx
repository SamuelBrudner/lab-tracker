import * as React from "react";

import { OperationRow } from "./OperationRow.jsx";
import { DictateText } from "./DictateText.jsx";
import { ReasonChips } from "./ReasonChips.jsx";
import { operationProposalText, operationIntent } from "./format.js";

const LANES = [
  ["judgment", "Needs your judgment"],
  ["confirmations", "Quick confirmations"],
  ["later", "Can wait"],
];

function TriageReview({ changeSet, workflow, sourceReview, sourcePreviews, onShowProposals, onPause, onCarryover, initialGroupId = "" }) {
  const groups = changeSet.triage?.groups || [];
  const summary = changeSet.triage?.summary;
  const [chosenLane, setChosenLane] = React.useState(() => groups.find((item) => item.group_id === initialGroupId)?.lane || null);
  const [chosenGroup, setChosenGroup] = React.useState(initialGroupId);
  const [correcting, setCorrecting] = React.useState(false);
  const [rejecting, setRejecting] = React.useState(false);
  const [answers, setAnswers] = React.useState({});
  const defaultLane = LANES.find(([key]) => groups.some((group) => !group.resolved && group.lane === key))?.[0] || "judgment";
  const lane = chosenLane || defaultLane;
  const visible = groups.filter((group) => !group.resolved && group.lane === lane);
  const group = visible.find((item) => item.group_id === chosenGroup) || visible[0];
  const members = group ? group.operation_ids.map((id) => changeSet.operations.find((op) => op.operation_id === id)).filter(Boolean) : [];
  const pending = Object.values(workflow.pendingCommands).some(Boolean);
  const actionable = (summary?.judgment || 0) + (summary?.confirmations || 0);
  const headline = changeSet.status === "committed" ? "Review committed"
    : changeSet.status === "rejected" ? "Review finished without kept graph changes"
    : actionable ? `${actionable} ${actionable === 1 ? "decision needs" : "decisions need"} your input`
    : groups.length ? "Your priority decisions are saved" : "No graph decisions need review";

  function selectLane(key) {
    setChosenLane(key);
    setChosenGroup("");
    setCorrecting(false);
    setRejecting(false);
  }

  async function decide(decision, reason = null) {
    const success = group.kind === "clarification"
      ? await workflow.decideTriageClarification(group, decision === "accepted" ? "answered" : decision, answers[group.group_id] || group.answer || null)
      : await workflow.decideTriageGroup(group, decision, reason);
    if (success) {
      setChosenGroup("");
      setChosenLane(null);
      setCorrecting(false);
      setRejecting(false);
    }
  }

  if (!summary) {
    return <div className="review-triage"><p>Triage is unavailable for this draft. Review its proposals directly.</p><button type="button" className="btn-primary" onClick={onShowProposals}>Review proposals</button></div>;
  }
  return (
    <section className="review-triage" aria-label="Review triage">
      <div className="triage-lead" role="status">
        <h3>{headline}</h3>
        <p className="subtle">{summary.deferred ? `${summary.deferred} ${summary.deferred === 1 ? "decision is" : "decisions are"} saved for later. ` : ""}Keep what reflects the work, correct the meaning, or return later.</p>
        <button type="button" className="btn-link" onClick={onPause}>Save progress and return to queue</button>
      </div>
      <div className="review-view-toggle triage-lanes" role="group" aria-label="Triage lanes">
        {LANES.map(([key, label]) => <button key={key} type="button" aria-pressed={lane === key} onClick={() => selectLane(key)}>{label} ({summary[key] || 0})</button>)}
      </div>
      {group ? (
        <article className="triage-decision" key={group.group_id}>
          <div className="item-head">
            <span className="subtle">Decision {visible.indexOf(group) + 1} of {visible.length}</span>
            {visible.length > 1 ? <select aria-label="Choose a triage decision" value={group.group_id} onChange={(event) => { setChosenGroup(event.target.value); setCorrecting(false); setRejecting(false); }}>{visible.map((item, index) => <option key={item.group_id} value={item.group_id}>Decision {index + 1}: {item.statement || item.question}</option>)}</select> : null}
          </div>
          <h3>{group.question}</h3>
          <p className="subtle">{group.reason}</p>
          {group.kind === "clarification" ? <label>Your answer<textarea value={answers[group.group_id] ?? group.answer ?? ""} disabled={!workflow.canEditDraft || pending} onChange={(event) => setAnswers((current) => ({ ...current, [group.group_id]: event.target.value }))} /></label> : null}
          {group.kind === "clarification" ? <DictateText key={group.group_id} label="Dictate answer" disabled={!workflow.canEditDraft || pending} onText={(text) => setAnswers((current) => ({ ...current, [group.group_id]: text }))} /> : null}
          <div className="triage-changes">
            {members.map((operation) => <div key={operation.operation_id}><strong>{operationIntent(operation)}</strong><p>{operationProposalText(operation, workflow.payloads)}</p>{operation.rationale ? <p className="subtle">{operation.rationale}</p> : null}{(operation.source_refs || []).map((ref, index) => ref.quote ? <blockquote className="source-snippet" key={index}>{ref.quote}</blockquote> : null)}</div>)}
          </div>
          {members.length ? <p className="subtle">{members.length} {members.length === 1 ? "graph edit" : "related graph edits"}. Keeping this decision records explicit acceptance; an owner commits the graph changes.</p> : null}
          <div className="inline triage-actions">
            {group.deferred ? <button type="button" className="btn-primary" disabled={!workflow.canEditDraft || pending} onClick={() => decide("resume")}>Return to review</button> : <>
              <button type="button" className="btn-primary" disabled={!workflow.canEditDraft || pending || (group.kind === "clarification" && !(answers[group.group_id] || group.answer || "").trim())} onClick={() => decide("accepted")}>{group.kind === "clarification" ? "Save answer" : lane === "confirmations" ? "Confirm these links" : "Keep this decision"}</button>
              {group.kind !== "clarification" ? <button type="button" className="btn-secondary" disabled={!workflow.canEditDraft || pending} onClick={() => setCorrecting((value) => !value)} aria-expanded={correcting}>Correct</button> : null}
              <button type="button" className="btn-secondary" disabled={!workflow.canEditDraft || pending} onClick={() => decide("deferred")}>Later</button>
              {group.kind !== "clarification" ? <button type="button" className="btn-link" disabled={!workflow.canEditDraft || pending} onClick={() => setRejecting((value) => !value)}>Reject decision</button> : null}
            </>}
          </div>
          {rejecting ? <ReasonChips onChoose={(reason) => decide("rejected", reason)} disabled={pending} onCancel={() => setRejecting(false)} /> : null}
          {members.length ? <details className="context-details" open={correcting} onToggle={(event) => setCorrecting(event.currentTarget.open)}>
            <summary>Evidence and corrections</summary>
            {members.map((operation) => <OperationRow key={operation.operation_id} operation={operation} changeSet={changeSet} payloadText={workflow.payloads[operation.operation_id]} reviewNote={workflow.operationReviewNotes[operation.operation_id]} canEditDraft={workflow.canEditDraft && !pending} pending={pending} sourceArtifacts={sourceReview.byOperationId[operation.operation_id]?.artifacts || []} sourcePreviews={sourcePreviews} onPatchOperationPayload={workflow.patchOperationPayload} onUpdatePayloadText={workflow.updatePayloadText} onUpdateOperationReviewNote={workflow.updateOperationReviewNote} onSaveOperation={workflow.saveOperation} showDecisionActions={false} />)}
          </details> : null}
        </article>
      ) : <p className="subtle">Nothing is waiting in this lane.</p>}
      {changeSet.status === "submitted" ? <p role="status">Your decisions are saved. Awaiting owner commit.</p> : null}
      {changeSet.review_metadata?.review_triage_carryover_to ? <div role="status"><p>Your kept decisions are committed. Unfinished items remain in a follow-up review.</p><button type="button" className="btn-secondary" onClick={() => onCarryover(changeSet.review_metadata.review_triage_carryover_to)}>Continue deferred review</button></div> : null}
    </section>
  );
}

export { TriageReview };
