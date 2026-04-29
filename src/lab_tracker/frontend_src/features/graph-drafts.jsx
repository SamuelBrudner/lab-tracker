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
  return `${operation.op} ${operation.entity_type}`;
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

function sourceRefText(ref) {
  const label = ref?.label ? `${ref.label}: ` : "";
  const quote = ref?.quote || "";
  return `${label}${quote}` || "Source reference";
}

function GraphDraftDetailCard({ token, changeSetId, navigate, canWrite, setBusy, setFlash }) {
  const [changeSet, setChangeSet] = useState(null);
  const [payloads, setPayloads] = useState({});
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [sourceImage, setSourceImage] = useState("");
  const [commitMessage, setCommitMessage] = useState("");

  const acceptedCount = useMemo(
    () =>
      (changeSet?.operations || []).filter((operation) => operation.status === "accepted")
        .length,
    [changeSet]
  );

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
    if (!changeSet || !canWrite) {
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
              <span className="pill">{changeSet.model}</span>
              <span className="pill">{changeSet.provider}</span>
            </div>
            {sourceImage ? (
              <img className="note-image" src={sourceImage} alt={changeSet.source_filename || "Source note"} />
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
              <div>
                <div className="subtle">Created</div>
                <div className="mono">{formatDate(changeSet.created_at)}</div>
              </div>
              {changeSet.error_metadata?.message ? (
                <p className="flash error">{changeSet.error_metadata.message}</p>
              ) : null}
            </div>
          </section>

          <section className="review-pane">
            <div className="item-head">
              <h3>Operations</h3>
              <button
                type="button"
                className="btn-secondary"
                disabled={!canWrite || changeSet.status !== "ready"}
                onClick={acceptAll}
              >
                Accept all
              </button>
            </div>
            <div className="stack">
              {(changeSet.operations || []).map((operation) => (
                <article className="item" key={operation.operation_id}>
                  <div className="item-head">
                    <strong>{operationTitle(operation)}</strong>
                    <span className={statusClass(operation.status)}>{operation.status}</span>
                  </div>
                  <div className="inline">
                    {operation.client_ref ? <span className="pill">{operation.client_ref}</span> : null}
                    {operation.confidence !== null && operation.confidence !== undefined ? (
                      <span className="pill">{Math.round(operation.confidence * 100)}%</span>
                    ) : null}
                    {operation.result_entity_id ? (
                      <span className="pill mono">{operation.result_entity_id}</span>
                    ) : null}
                  </div>
                  {operation.rationale ? <p>{operation.rationale}</p> : null}
                  {(operation.source_refs || []).map((ref, index) => (
                    <p className="source-snippet" key={`${operation.operation_id}-${index}`}>
                      {sourceRefText(ref)}
                    </p>
                  ))}
                  <label>
                    Payload JSON
                    <textarea
                      className="mono"
                      value={payloads[operation.operation_id] || ""}
                      onChange={(event) =>
                        updatePayloadText(operation.operation_id, event.target.value)
                      }
                      disabled={!canWrite || changeSet.status !== "ready"}
                    />
                  </label>
                  {operation.error_metadata?.message ? (
                    <p className="flash error">{operation.error_metadata.message}</p>
                  ) : null}
                  <div className="inline">
                    <button
                      type="button"
                      className="btn-primary"
                      disabled={!canWrite || changeSet.status !== "ready"}
                      onClick={() => saveOperation(operation, "accepted")}
                    >
                      Accept
                    </button>
                    <button
                      type="button"
                      className="btn-secondary"
                      disabled={!canWrite || changeSet.status !== "ready"}
                      onClick={() => saveOperation(operation, "proposed")}
                    >
                      Save
                    </button>
                    <button
                      type="button"
                      className="btn-danger"
                      disabled={!canWrite || changeSet.status !== "ready"}
                      onClick={() => saveOperation(operation, "rejected")}
                    >
                      Reject
                    </button>
                  </div>
                </article>
              ))}
            </div>

            <form className="form" onSubmit={commitDraft}>
              <label>
                Commit message
                <input
                  value={commitMessage}
                  onChange={(event) => setCommitMessage(event.target.value)}
                  disabled={!canWrite || changeSet.status !== "ready"}
                />
              </label>
              <button
                className="btn-primary"
                disabled={!canWrite || changeSet.status !== "ready" || acceptedCount === 0}
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
