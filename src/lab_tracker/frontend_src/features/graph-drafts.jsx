import * as React from "react";

import { apiRequest } from "../shared/api.js";
import { formatDate } from "../shared/formatters.js";

const { useCallback, useEffect, useMemo, useState } = React;

function statusClass(status) {
  if (status === "accepted" || status === "applied" || status === "committed") {
    return "pill review-approved";
  }
  if (status === "rejected" || status === "failed") {
    return "pill review-rejected";
  }
  return "pill review-pending";
}

function operationTitle(operation) {
  return operation.semantic_type
    ? operation.semantic_type.replaceAll("_", " ")
    : `${operation.op} ${operation.entity_type}`;
}

function operationIntent(operation) {
  const semanticType = operation.semantic_type || "";
  if (semanticType.startsWith("link_note_to_")) {
    return `Proposed link to existing ${semanticType.replace("link_note_to_", "")}`;
  }
  if (semanticType === "suggest_new_question" || semanticType === "suggest_new_dataset") {
    return `Proposed new ${operation.entity_type}`;
  }
  if (semanticType === "create_note" || semanticType === "suggest_followup") {
    return "Proposed research note";
  }
  if (semanticType === "request_clarification") {
    return "Clarification request";
  }
  if (operation.op === "create") {
    return `Proposed new ${operation.entity_type}`;
  }
  return `Proposed ${operation.entity_type} update`;
}

function payloadText(changeSet) {
  const entries = {};
  for (const operation of changeSet?.operations || []) {
    entries[operation.operation_id] = JSON.stringify(operation.payload || {}, null, 2);
  }
  return entries;
}

function imageDataUrl(raw) {
  if (!raw || !raw.content_base64 || !raw.content_type) {
    return "";
  }
  return `data:${raw.content_type};base64,${raw.content_base64}`;
}

function sourceRegionStyle(region) {
  if (!region || typeof region !== "object") {
    return null;
  }
  const { height, width, x, y } = region;
  const values = [x, y, width, height];
  if (values.some((value) => typeof value !== "number" || Number.isNaN(value) || value < 0)) {
    return null;
  }
  const multiplier = values.every((value) => value <= 1) ? 100 : 1;
  const left = x * multiplier;
  const top = y * multiplier;
  const boxWidth = width * multiplier;
  const boxHeight = height * multiplier;
  if (left > 100 || top > 100 || boxWidth <= 0 || boxHeight <= 0) {
    return null;
  }
  return {
    height: `${Math.min(boxHeight, 100 - top)}%`,
    left: `${left}%`,
    top: `${top}%`,
    width: `${Math.min(boxWidth, 100 - left)}%`,
  };
}

function sourceRegions(changeSet) {
  const regions = [];
  for (const operation of changeSet?.operations || []) {
    for (const ref of operation.source_refs || []) {
      const style = sourceRegionStyle(ref?.region);
      if (style) {
        regions.push({ operation, ref, style });
      }
    }
  }
  return regions;
}

function sourceRefText(ref) {
  const label = ref?.label ? `${ref.label}: ` : "";
  const quote = ref?.quote || "";
  return `${label}${quote}` || "Source reference";
}

function contextCountLabel(key) {
  return key.replaceAll("_", " ");
}

function semanticLinkTargetType(operation) {
  if (operation.semantic_type === "link_note_to_question") {
    return "question";
  }
  if (operation.semantic_type === "link_note_to_session") {
    return "session";
  }
  if (operation.semantic_type === "link_note_to_dataset") {
    return "dataset";
  }
  if (operation.semantic_type === "link_note_to_analysis") {
    return "analysis";
  }
  return "";
}

function contextOptions(changeSet, entityType) {
  const context = changeSet?.context_packet || {};
  if (entityType === "question") {
    return context.active_or_staged_questions || [];
  }
  if (entityType === "session") {
    return context.recent_sessions || [];
  }
  if (entityType === "dataset") {
    return context.recent_datasets || [];
  }
  if (entityType === "analysis") {
    return context.recent_analyses || [];
  }
  return [];
}

function payloadTargetId(payload, entityType) {
  const targets = Array.isArray(payload?.targets) ? payload.targets : [];
  const match = targets.find((target) => target.entity_type === entityType);
  return typeof match?.entity_id === "string" ? match.entity_id : "";
}

function nextPayloadWithTarget(payload, entityType, entityId) {
  const targets = Array.isArray(payload?.targets) ? [...payload.targets] : [];
  const filtered = targets.filter((target) => target.entity_type !== entityType);
  if (entityId) {
    filtered.push({ entity_id: entityId, entity_type: entityType });
  }
  return { ...(payload || {}), targets: filtered };
}

function parsedPayloadFromText(raw) {
  try {
    const parsed = JSON.parse(raw || "{}");
    return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed : null;
  } catch {
    return null;
  }
}

function GraphDraftDetailCard({
  token,
  changeSetId,
  navigate,
  canWrite,
  canManageGraph = false,
  setBusy,
  setFlash,
}) {
  const [changeSet, setChangeSet] = useState(null);
  const [payloads, setPayloads] = useState({});
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [sourceImage, setSourceImage] = useState("");
  const [commitMessage, setCommitMessage] = useState("");
  const [reviewNote, setReviewNote] = useState("");

  const acceptedCount = useMemo(
    () =>
      (changeSet?.operations || []).filter((operation) => operation.status === "accepted")
        .length,
    [changeSet]
  );
  const visibleSourceRegions = useMemo(() => sourceRegions(changeSet), [changeSet]);
  const canEditDraft =
    canWrite && ["ready", "changes_requested"].includes(changeSet?.status || "");
  const canSubmitDraft =
    canWrite && ["ready", "changes_requested"].includes(changeSet?.status || "");
  const canReviewDraft = canManageGraph && changeSet?.status === "submitted";
  const canCommitDraft =
    canManageGraph && ["ready", "submitted"].includes(changeSet?.status || "");

  const loadDraft = useCallback(async () => {
    if (!changeSetId) {
      return;
    }
    setLoading(true);
    setError("");
    try {
      const nextChangeSet = await apiRequest(`/graph-drafts/${changeSetId}`, { token });
      setChangeSet(nextChangeSet);
      setPayloads(payloadText(nextChangeSet));
      setCommitMessage(nextChangeSet?.commit_message || "");
      setReviewNote(nextChangeSet?.review_note || "");
    } catch (err) {
      setError(err.message || "Failed to load graph draft.");
    } finally {
      setLoading(false);
    }
  }, [changeSetId, token]);

  useEffect(() => {
    loadDraft();
  }, [loadDraft]);

  useEffect(() => {
    let canceled = false;
    setSourceImage("");
    const contentType = changeSet?.source_content_type || "";
    if (!changeSet?.source_note_id || !contentType.startsWith("image/")) {
      return () => {
        canceled = true;
      };
    }
    apiRequest(`/notes/${changeSet.source_note_id}/raw`, { token })
      .then((raw) => {
        if (!canceled) {
          setSourceImage(imageDataUrl(raw));
        }
      })
      .catch(() => {
        if (!canceled) {
          setSourceImage("");
        }
      });
    return () => {
      canceled = true;
    };
  }, [changeSet, token]);

  function updatePayloadText(operationId, value) {
    setPayloads((current) => ({ ...current, [operationId]: value }));
  }

  function patchOperationPayload(operation, patcher) {
    const current = parsedPayloadFromText(payloads[operation.operation_id]);
    if (!current) {
      setFlash("", "Operation payload must be valid JSON before using typed controls.");
      return;
    }
    const nextPayload = patcher(current);
    updatePayloadText(operation.operation_id, JSON.stringify(nextPayload, null, 2));
  }

  async function saveOperation(operation, nextStatus = operation.status) {
    let parsedPayload;
    try {
      parsedPayload = JSON.parse(payloads[operation.operation_id] || "{}");
    } catch {
      setFlash("", "Operation payload must be valid JSON.");
      return;
    }
    if (!parsedPayload || typeof parsedPayload !== "object" || Array.isArray(parsedPayload)) {
      setFlash("", "Operation payload must be a JSON object.");
      return;
    }
    setBusy(true);
    setFlash("", "");
    try {
      const nextChangeSet = await apiRequest(
        `/graph-drafts/${changeSet.change_set_id}/operations/${operation.operation_id}`,
        {
          body: {
            payload: parsedPayload,
            status: nextStatus,
          },
          method: "PATCH",
          token,
        }
      );
      setChangeSet(nextChangeSet);
      setPayloads(payloadText(nextChangeSet));
      setFlash("Graph draft operation updated.");
    } catch (err) {
      setFlash("", err.message || "Failed to update graph draft operation.");
    } finally {
      setBusy(false);
    }
  }

  async function acceptAll() {
    if (!changeSet) {
      return;
    }
    for (const operation of changeSet.operations || []) {
      if (operation.status === "applied") {
        continue;
      }
      await saveOperation(operation, "accepted");
    }
  }

  async function commitDraft(event) {
    event.preventDefault();
    if (!changeSet || !canCommitDraft) {
      return;
    }
    if (!commitMessage.trim()) {
      setFlash("", "Commit message is required.");
      return;
    }
    setBusy(true);
    setFlash("", "");
    try {
      const nextChangeSet = await apiRequest(`/graph-drafts/${changeSet.change_set_id}/commit`, {
        body: { message: commitMessage.trim() },
        method: "POST",
        token,
      });
      setChangeSet(nextChangeSet);
      setPayloads(payloadText(nextChangeSet));
      setFlash("Graph draft committed.");
    } catch (err) {
      setFlash("", err.message || "Failed to commit graph draft.");
    } finally {
      setBusy(false);
    }
  }

  async function submitDraft() {
    if (!changeSet || !canSubmitDraft) {
      return;
    }
    setBusy(true);
    setFlash("", "");
    try {
      const nextChangeSet = await apiRequest(`/graph-drafts/${changeSet.change_set_id}/submit`, {
        method: "POST",
        token,
      });
      setChangeSet(nextChangeSet);
      setFlash("Graph draft submitted for review.");
    } catch (err) {
      setFlash("", err.message || "Failed to submit graph draft.");
    } finally {
      setBusy(false);
    }
  }

  async function reviewDraft(status) {
    if (!changeSet || !canReviewDraft) {
      return;
    }
    setBusy(true);
    setFlash("", "");
    try {
      const nextChangeSet = await apiRequest(`/graph-drafts/${changeSet.change_set_id}/review`, {
        body: { status, note: reviewNote.trim() || null },
        method: "POST",
        token,
      });
      setChangeSet(nextChangeSet);
      setFlash(status === "rejected" ? "Graph draft rejected." : "Changes requested.");
    } catch (err) {
      setFlash("", err.message || "Failed to review graph draft.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <article className="card span-12">
      <div className="item-head">
        <h2>Graph Draft Review</h2>
        {loading ? <span className="pill">Loading...</span> : null}
      </div>
      {error ? <p className="flash error">{error}</p> : null}

      {changeSet ? (
        <div className="review-layout">
          <section className="review-pane">
            <div className="inline">
              <span className={statusClass(changeSet.status)}>{changeSet.status}</span>
              <span className="pill">{changeSet.draft_mode || "graph_context"}</span>
              <span className="pill">{changeSet.model}</span>
              <span className="pill">{changeSet.provider}</span>
            </div>
            {sourceImage ? (
              <div className="source-image-frame">
                <img
                  className="note-image"
                  src={sourceImage}
                  alt={changeSet.source_filename || "Source note"}
                />
                {visibleSourceRegions.map((region, index) => (
                  <div
                    aria-label={`Source region ${index + 1}: ${
                      region.ref?.label || operationTitle(region.operation)
                    }`}
                    className="source-region-box"
                    key={`${region.operation.operation_id}-${index}`}
                    style={region.style}
                    title={sourceRefText(region.ref)}
                  >
                    <span>{index + 1}</span>
                  </div>
                ))}
              </div>
            ) : null}
            <div className="stack">
              <div>
                <div className="subtle">Source note</div>
                <div className="mono">{changeSet.source_note_id}</div>
              </div>
              <div>
                <div className="subtle">Source file</div>
                <div className="mono">{changeSet.source_filename || "(none)"}</div>
              </div>
              {(changeSet.context_packet?.source_artifacts || []).length > 0 ? (
                <div>
                  <div className="subtle">Source artifacts and transcripts</div>
                  <div className="stack">
                    {changeSet.context_packet.source_artifacts.map((artifact) => (
                      <article className="item" key={artifact.note_id || artifact.artifact_id}>
                        <div className="inline">
                          <span className="pill">{artifact.type || "source"}</span>
                          {artifact.content_type ? (
                            <span className="pill">{artifact.content_type}</span>
                          ) : null}
                        </div>
                        <p className="mono">{artifact.filename || artifact.note_id}</p>
                        {artifact.transcript_text ? (
                          <p className="source-snippet">{artifact.transcript_text}</p>
                        ) : null}
                      </article>
                    ))}
                  </div>
                </div>
              ) : null}
              <div>
                <div className="subtle">Created</div>
                <div className="mono">
                  {formatDate(changeSet.created_at)}
                  {changeSet.created_by_username ? ` by ${changeSet.created_by_username}` : ""}
                </div>
              </div>
              {changeSet.submitted_at ? (
                <div>
                  <div className="subtle">Submitted</div>
                  <div className="mono">
                    {formatDate(changeSet.submitted_at)}
                    {changeSet.submitted_by_username
                      ? ` by ${changeSet.submitted_by_username}`
                      : ""}
                  </div>
                </div>
              ) : null}
              {changeSet.reviewed_at ? (
                <div>
                  <div className="subtle">Reviewed</div>
                  <div className="mono">
                    {formatDate(changeSet.reviewed_at)}
                    {changeSet.reviewed_by_username
                      ? ` by ${changeSet.reviewed_by_username}`
                      : ""}
                  </div>
                  {changeSet.review_note ? <p>{changeSet.review_note}</p> : null}
                </div>
              ) : null}
              {changeSet.committed_at ? (
                <div>
                  <div className="subtle">Committed</div>
                  <div className="mono">
                    {formatDate(changeSet.committed_at)}
                    {changeSet.committed_by_username
                      ? ` by ${changeSet.committed_by_username}`
                      : ""}
                  </div>
                </div>
              ) : null}
              <div className="inline">
                <button
                  type="button"
                  className="btn-secondary"
                  disabled={!canSubmitDraft}
                  onClick={submitDraft}
                >
                  Submit for review
                </button>
              </div>
              {changeSet.error_metadata?.message ? (
                <p className="flash error">{changeSet.error_metadata.message}</p>
              ) : null}
              {changeSet.summary ? (
                <div>
                  <div className="subtle">Draft summary</div>
                  <p>{changeSet.summary}</p>
                </div>
              ) : null}
              {(changeSet.uncertain_fields || []).length > 0 ? (
                <div>
                  <div className="subtle">Uncertain fields</div>
                  <ul className="compact-list">
                    {changeSet.uncertain_fields.map((item) => (
                      <li key={item}>{item}</li>
                    ))}
                  </ul>
                </div>
              ) : null}
              {(changeSet.clarification_requests || []).length > 0 ? (
                <div>
                  <div className="subtle">Clarification requests</div>
                  <ul className="compact-list">
                    {changeSet.clarification_requests.map((item) => (
                      <li key={item}>{item}</li>
                    ))}
                  </ul>
                </div>
              ) : null}
              {changeSet.context_packet?.context_summary ? (
                <div>
                  <div className="subtle">Context summary</div>
                  <div className="inline">
                    <span className="pill">
                      ~{changeSet.context_packet.context_summary.approximate_size_bytes || 0} bytes
                    </span>
                    {Object.entries(changeSet.context_packet.context_summary.counts || {}).map(
                      ([key, value]) => (
                        <span className="pill" key={key}>
                          {contextCountLabel(key)}: {value}
                        </span>
                      )
                    )}
                  </div>
                  {Object.keys(
                    changeSet.context_packet.context_summary.source_artifact_counts || {}
                  ).length > 0 ? (
                    <p className="subtle">
                      Source artifacts:{" "}
                      {Object.entries(
                        changeSet.context_packet.context_summary.source_artifact_counts
                      )
                        .map(([key, value]) => `${key} ${value}`)
                        .join(", ")}
                    </p>
                  ) : null}
                  {(changeSet.context_packet.context_summary.selected_targets || []).length > 0 ? (
                    <ul className="compact-list">
                      {changeSet.context_packet.context_summary.selected_targets.map((target) => (
                        <li key={`${target.entity_type}-${target.entity_id}`}>
                          {target.entity_type}: {target.label || target.entity_id}
                        </li>
                      ))}
                    </ul>
                  ) : null}
                  {(changeSet.context_packet.context_summary.warnings || []).length > 0 ? (
                    <ul className="compact-list">
                      {changeSet.context_packet.context_summary.warnings.map((warning) => (
                        <li key={warning}>{warning}</li>
                      ))}
                    </ul>
                  ) : null}
                </div>
              ) : null}
              {changeSet.context_packet ? (
                <details className="context-details">
                  <summary>Graph context used</summary>
                  <pre className="manifest-preview">
                    {JSON.stringify(changeSet.context_packet, null, 2)}
                  </pre>
                </details>
              ) : null}
            </div>
          </section>

          <section className="review-pane">
            <div className="item-head">
              <h3>Operations</h3>
              <button
                type="button"
                className="btn-secondary"
                disabled={!canEditDraft}
                onClick={acceptAll}
              >
                Accept all
              </button>
            </div>
            <div className="stack">
              {(changeSet.operations || []).map((operation) => (
                <article className="item graph-operation-card" key={operation.operation_id}>
                  <div className="item-head">
                    <strong>{operationTitle(operation)}</strong>
                    <span className={statusClass(operation.status)}>{operation.status}</span>
                  </div>
                  <div className="inline">
                    <span className="pill">{operationIntent(operation)}</span>
                    {operation.client_ref ? <span className="pill">{operation.client_ref}</span> : null}
                    {operation.confidence !== null && operation.confidence !== undefined ? (
                      <span className="pill">{Math.round(operation.confidence * 100)}%</span>
                    ) : null}
                    {operation.result_entity_id ? (
                      <span className="pill mono">{operation.result_entity_id}</span>
                    ) : null}
                  </div>
                  {operation.rationale ? (
                    <div>
                      <div className="subtle">Model inference</div>
                      <p>{operation.rationale}</p>
                    </div>
                  ) : null}
                  {(operation.source_refs || []).length > 0 ? (
                    <div>
                      <div className="subtle">Source evidence</div>
                      {(operation.source_refs || []).map((ref, index) => (
                        <p className="source-snippet" key={`${operation.operation_id}-${index}`}>
                          {sourceRefText(ref)}
                        </p>
                      ))}
                    </div>
                  ) : null}
                  {semanticLinkTargetType(operation) ? (
                    <label>
                      Link target
                      <select
                        disabled={!canEditDraft}
                        onChange={(event) =>
                          patchOperationPayload(operation, (payload) =>
                            nextPayloadWithTarget(
                              payload,
                              semanticLinkTargetType(operation),
                              event.target.value
                            )
                          )
                        }
                        value={payloadTargetId(
                          parsedPayloadFromText(payloads[operation.operation_id]) || {},
                          semanticLinkTargetType(operation)
                        )}
                      >
                        <option value="">No linked {semanticLinkTargetType(operation)}</option>
                        {contextOptions(changeSet, semanticLinkTargetType(operation)).map((item) => (
                          <option key={item.id} value={item.id}>
                            {item.label || item.id}
                          </option>
                        ))}
                      </select>
                    </label>
                  ) : null}
                  {Object.prototype.hasOwnProperty.call(operation.payload || {}, "text") ? (
                    <label>
                      Text
                      <textarea
                        value={
                          parsedPayloadFromText(payloads[operation.operation_id])?.text || ""
                        }
                        disabled={!canEditDraft}
                        onChange={(event) =>
                          patchOperationPayload(operation, (payload) => ({
                            ...payload,
                            text: event.target.value,
                          }))
                        }
                      />
                    </label>
                  ) : null}
                  {Object.prototype.hasOwnProperty.call(operation.payload || {}, "raw_content") ? (
                    <label>
                      Note text
                      <textarea
                        value={
                          parsedPayloadFromText(payloads[operation.operation_id])?.raw_content || ""
                        }
                        disabled={!canEditDraft}
                        onChange={(event) =>
                          patchOperationPayload(operation, (payload) => ({
                            ...payload,
                            raw_content: event.target.value,
                          }))
                        }
                      />
                    </label>
                  ) : null}
                  <details className="context-details advanced-json">
                    <summary>Payload JSON (advanced)</summary>
                    <label>
                      Edit JSON payload
                      <textarea
                        className="mono"
                        value={payloads[operation.operation_id] || ""}
                        onChange={(event) =>
                          updatePayloadText(operation.operation_id, event.target.value)
                        }
                        disabled={!canEditDraft}
                      />
                    </label>
                  </details>
                  {operation.error_metadata?.message ? (
                    <p className="flash error">{operation.error_metadata.message}</p>
                  ) : null}
                  <div className="inline">
                    <button
                      type="button"
                      className="btn-primary"
                      disabled={!canEditDraft}
                      onClick={() => saveOperation(operation, "accepted")}
                    >
                      Accept
                    </button>
                    <button
                      type="button"
                      className="btn-secondary"
                      disabled={!canEditDraft}
                      onClick={() => saveOperation(operation, "proposed")}
                    >
                      Defer
                    </button>
                    <button
                      type="button"
                      className="btn-danger"
                      disabled={!canEditDraft}
                      onClick={() => saveOperation(operation, "rejected")}
                    >
                      Reject
                    </button>
                  </div>
                </article>
              ))}
            </div>

            <form className="form" onSubmit={commitDraft}>
              {canReviewDraft ? (
                <label>
                  Review note
                  <textarea
                    value={reviewNote}
                    onChange={(event) => setReviewNote(event.target.value)}
                  />
                </label>
              ) : null}
              {canReviewDraft ? (
                <div className="inline">
                  <button
                    type="button"
                    className="btn-secondary"
                    onClick={() => reviewDraft("changes_requested")}
                  >
                    Request changes
                  </button>
                  <button
                    type="button"
                    className="btn-danger"
                    onClick={() => reviewDraft("rejected")}
                  >
                    Reject draft
                  </button>
                </div>
              ) : null}
              <label>
                Commit message
                <input
                  value={commitMessage}
                  onChange={(event) => setCommitMessage(event.target.value)}
                  disabled={!canCommitDraft}
                />
              </label>
              <button
                className="btn-primary"
                disabled={!canCommitDraft || acceptedCount === 0}
              >
                Commit accepted changes
              </button>
            </form>
          </section>
        </div>
      ) : null}

      <div className="inline detail-actions">
        <button type="button" className="btn-secondary" onClick={() => navigate("/app")}>
          Back
        </button>
      </div>
    </article>
  );
}

export { GraphDraftDetailCard };
