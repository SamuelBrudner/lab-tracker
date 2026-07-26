import * as React from "react";

import {
  contextOptions,
  nextPayloadWithTarget,
  operationIntent,
  operationTitle,
  parsedPayloadFromText,
  payloadTargetId,
  semanticLinkTargetType,
  sourceRefText,
  statusClass,
} from "./format.js";
import { SourceArtifactEvidence } from "./SourceArtifactEvidence.jsx";

// Presentational per-proposal editor: proposal body, evidence, typed and raw
// payload edits, and the accept/defer/reject decision controls. All edit and
// decision handlers come from the workflow controller.
function OperationRow({
  operation,
  changeSet,
  payloadText,
  reviewNote,
  canEditDraft,
  pending,
  sourceArtifacts = [],
  sourcePreviews = {},
  usesSharedSourceEvidence = false,
  onPatchOperationPayload,
  onUpdatePayloadText,
  onUpdateOperationReviewNote,
  onSaveOperation,
}) {
  const parsed = parsedPayloadFromText(payloadText) || operation.payload || {};
  const proposed =
    parsed.text || parsed.raw_content || parsed.label || parsed.prompt || parsed.statement || "";
  const linkType = semanticLinkTargetType(operation);
  return (
    <div className="review-proposal">
      <div className="review-proposal-body">
        <p className="review-proposal-intent subtle">{operationIntent(operation)}</p>
        <p className="review-proposal-text">{proposed || operationTitle(operation)}</p>
        {operation.rationale ? (
          <p className="review-because">
            <span className="subtle">Model inference</span> {operation.rationale}
            {operation.confidence !== null && operation.confidence !== undefined
              ? ` · ${Math.round(operation.confidence * 100)}% confident`
              : ""}
          </p>
        ) : null}
        {(operation.source_refs || []).length > 0 ? (
          <div className="review-evidence">
            <div className="subtle">Source evidence</div>
            {(operation.source_refs || []).map((ref, index) => (
              <p className="source-snippet" key={`${operation.operation_id}-${index}`}>
                {sourceRefText(ref)}
              </p>
            ))}
          </div>
        ) : null}
        <SourceArtifactEvidence
          artifacts={sourceArtifacts}
          previews={sourcePreviews}
          operation={operation}
        />
        {usesSharedSourceEvidence ? (
          <p className="source-artifact-shared-link subtle">See shared source evidence above.</p>
        ) : null}
        {operation.error_metadata?.message ? (
          <p className="flash error">{operation.error_metadata.message}</p>
        ) : null}
        {operation.result_entity_id ? (
          <p className="review-result subtle">
            Created <span className="mono">{operation.result_entity_id}</span>
          </p>
        ) : null}
        <details className="context-details review-edit">
          <summary>Edit this proposal</summary>
          <div className="stack">
            {linkType ? (
              <label>
                Link target
                <select
                  disabled={!canEditDraft}
                  onChange={(event) =>
                    onPatchOperationPayload(operation, (payload) =>
                      nextPayloadWithTarget(payload, linkType, event.target.value)
                    )
                  }
                  value={payloadTargetId(parsedPayloadFromText(payloadText) || {}, linkType)}
                >
                  <option value="">No linked {linkType}</option>
                  {contextOptions(changeSet, linkType).map((item) => (
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
                  value={parsedPayloadFromText(payloadText)?.text || ""}
                  disabled={!canEditDraft}
                  onChange={(event) =>
                    onPatchOperationPayload(operation, (payload) => ({
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
                  value={parsedPayloadFromText(payloadText)?.raw_content || ""}
                  disabled={!canEditDraft}
                  onChange={(event) =>
                    onPatchOperationPayload(operation, (payload) => ({
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
                  value={payloadText || ""}
                  onChange={(event) =>
                    onUpdatePayloadText(operation.operation_id, event.target.value)
                  }
                  disabled={!canEditDraft}
                />
              </label>
            </details>
            <label>
              Decision note
              <textarea
                value={reviewNote || ""}
                disabled={!canEditDraft}
                onChange={(event) =>
                  onUpdateOperationReviewNote(operation.operation_id, event.target.value)
                }
              />
            </label>
            <div className="inline">
              <button
                type="button"
                className="btn-secondary"
                disabled={!canEditDraft || Boolean(pending)}
                onClick={() => onSaveOperation(operation)}
                title="Save your edits to this proposal without changing its decision"
              >
                Save edit
              </button>
            </div>
          </div>
        </details>
      </div>
      <aside className="review-proposal-actions">
        <span className={statusClass(operation.status)}>{operation.status}</span>
        <button
          type="button"
          className="btn-primary"
          disabled={!canEditDraft || Boolean(pending)}
          onClick={() => onSaveOperation(operation, "accepted")}
        >
          Accept
        </button>
        <button
          type="button"
          className="btn-secondary"
          disabled={!canEditDraft || Boolean(pending)}
          onClick={() => onSaveOperation(operation, "proposed")}
        >
          Defer
        </button>
        <button
          type="button"
          className="btn-danger"
          disabled={!canEditDraft || Boolean(pending)}
          onClick={() => onSaveOperation(operation, "rejected")}
        >
          Reject
        </button>
      </aside>
    </div>
  );
}

export { OperationRow };
