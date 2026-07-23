import * as React from "react";

import { apiListRequest, apiRequest, buildApiPath } from "../shared/api.js";
import { graphDrafts } from "../shared/gateways/index.js";
import {
  canContributeWithRole,
  canManageWithRole,
  operationReviewNoteText,
  parsedPayloadFromText,
  payloadText,
  spokenReviewScript,
} from "../features/graph-drafts/format.js";

const { useCallback, useEffect, useMemo, useRef, useState } = React;

// Data controller for the graph-draft review surface: owns the change-set,
// its edit buffers, project-access gating, the per-command in-flight guard, and
// every mutation command. The stale-route and pending guards live here so the
// view and the media console never mutate the wrong draft or double-submit.
function useGraphDraftWorkflow({
  token,
  changeSetId,
  canWrite,
  canManageGraph = false,
  user = null,
  setBusy,
  setFlash,
}) {
  const [changeSet, setChangeSet] = useState(null);
  const [payloads, setPayloads] = useState({});
  const [operationReviewNotes, setOperationReviewNotes] = useState({});
  const [draftProjectRole, setDraftProjectRole] = useState("");
  const [draftProjectId, setDraftProjectId] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [commitMessage, setCommitMessage] = useState("");
  const [reviewNote, setReviewNote] = useState("");
  // Last-started load wins: a superseded load's response is ignored.
  const loadGenerationRef = useRef(0);
  // Track the route id so we reset route-scoped state only on a genuine switch.
  const previousChangeSetIdRef = useRef(changeSetId);

  // Per-command in-flight state. The ref guards synchronously against a double
  // click before React re-renders; the state drives per-button disabling. Keyed
  // by command so one command settling never clears another's pending flag.
  const [pendingCommands, setPendingCommands] = useState({});
  const pendingCommandsRef = useRef({});
  const beginCommand = useCallback((name) => {
    if (pendingCommandsRef.current[name]) {
      return false;
    }
    pendingCommandsRef.current = { ...pendingCommandsRef.current, [name]: true };
    setPendingCommands((prev) => ({ ...prev, [name]: true }));
    return true;
  }, []);
  const endCommand = useCallback((name) => {
    pendingCommandsRef.current = { ...pendingCommandsRef.current, [name]: false };
    setPendingCommands((prev) => ({ ...prev, [name]: false }));
  }, []);

  const acceptedCount = useMemo(
    () =>
      (changeSet?.operations || []).filter((operation) => operation.status === "accepted").length,
    [changeSet]
  );
  const spokenReview = useMemo(() => spokenReviewScript(changeSet, payloads), [changeSet, payloads]);

  const isAdmin = user?.role === "admin";
  // The loaded draft is only actionable when it is the one the route points at;
  // during a route change (or a superseded load) the previous draft must not be
  // editable or targetable by a mutation.
  const loadedId = changeSet?.change_set_id ?? null;
  const isCurrent = loadedId !== null && loadedId === changeSetId;
  const usesDraftProjectAccess = Boolean(changeSet?.project_id);
  const hasDraftProjectAccess = usesDraftProjectAccess && draftProjectId === changeSet.project_id;
  const effectiveCanWrite = usesDraftProjectAccess
    ? isAdmin || (hasDraftProjectAccess && canContributeWithRole(user, draftProjectRole))
    : Boolean(canWrite);
  const effectiveCanManageGraph = usesDraftProjectAccess
    ? isAdmin || (hasDraftProjectAccess && canManageWithRole(user, draftProjectRole))
    : Boolean(canManageGraph);
  const canEditDraft =
    isCurrent &&
    effectiveCanWrite &&
    ["ready", "changes_requested"].includes(changeSet?.status || "");
  const canSubmitDraft =
    isCurrent &&
    effectiveCanWrite &&
    ["ready", "changes_requested"].includes(changeSet?.status || "");
  const canReviewDraft = isCurrent && effectiveCanManageGraph && changeSet?.status === "submitted";
  const canCommitDraft =
    isCurrent &&
    effectiveCanManageGraph &&
    ["ready", "submitted"].includes(changeSet?.status || "");

  const loadDraft = useCallback(async () => {
    if (!changeSetId) {
      return;
    }
    const requestedId = changeSetId;
    const generation = (loadGenerationRef.current += 1);
    setLoading(true);
    setError("");
    try {
      const nextChangeSet = await graphDrafts.getChangeSet(requestedId, { token });
      // Ignore a stale response if a newer load (or route change) superseded it.
      if (generation !== loadGenerationRef.current) {
        return;
      }
      setChangeSet(nextChangeSet);
      setPayloads(payloadText(nextChangeSet));
      setOperationReviewNotes(operationReviewNoteText(nextChangeSet));
      setCommitMessage(nextChangeSet?.commit_message || "");
      setReviewNote(nextChangeSet?.review_note || "");
    } catch (err) {
      if (generation !== loadGenerationRef.current) {
        return;
      }
      setError(err.message || "Failed to load graph draft.");
    } finally {
      if (generation === loadGenerationRef.current) {
        setLoading(false);
      }
    }
  }, [changeSetId, token]);

  useEffect(() => {
    // On a genuine route change to a different draft, drop all route-scoped
    // state immediately so the previous draft is neither shown nor actionable
    // while the next one loads. Same-id reloads (after a mutation) are left
    // untouched so in-progress edits on the current draft are preserved.
    if (previousChangeSetIdRef.current !== changeSetId) {
      previousChangeSetIdRef.current = changeSetId;
      loadGenerationRef.current += 1; // abandon any in-flight load for the old id
      setChangeSet(null);
      setPayloads({});
      setOperationReviewNotes({});
      setCommitMessage("");
      setReviewNote("");
      setError("");
    }
  }, [changeSetId]);

  useEffect(() => {
    loadDraft();
  }, [loadDraft]);

  useEffect(() => {
    let canceled = false;
    const projectId = changeSet?.project_id || "";
    if (!projectId) {
      setDraftProjectId("");
      setDraftProjectRole("");
      return () => {
        canceled = true;
      };
    }
    setDraftProjectId(projectId);
    if (user?.role === "admin") {
      setDraftProjectRole("owner");
      return () => {
        canceled = true;
      };
    }
    if (!user?.user_id) {
      setDraftProjectRole("");
      return () => {
        canceled = true;
      };
    }

    setDraftProjectRole("");
    apiListRequest(buildApiPath(`/projects/${projectId}/members`, { limit: 200 }), { token })
      .then(({ data }) => {
        if (canceled) {
          return;
        }
        const membership = data.find((member) => member.user_id === user.user_id);
        setDraftProjectRole(membership?.role || "");
      })
      .catch(() => {
        if (!canceled) {
          setDraftProjectRole("");
        }
      });
    return () => {
      canceled = true;
    };
  }, [changeSet?.project_id, token, user?.role, user?.user_id]);

  function updatePayloadText(operationId, value) {
    setPayloads((current) => ({ ...current, [operationId]: value }));
  }

  function updateOperationReviewNote(operationId, value) {
    setOperationReviewNotes((current) => ({ ...current, [operationId]: value }));
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
    // Never mutate an operation on a draft that is no longer the active route.
    if (!isCurrent) {
      return;
    }
    // Per-operation in-flight guard so each row is independently non-duplicable.
    const commandKey = `op:${operation.operation_id}`;
    if (!beginCommand(commandKey)) {
      return;
    }
    setBusy(true);
    setFlash("", "");
    try {
      const nextChangeSet = await apiRequest(
        `/graph-drafts/${changeSetId}/operations/${operation.operation_id}`,
        {
          body: {
            payload: parsedPayload,
            review_note: operationReviewNotes[operation.operation_id]?.trim() || null,
            status: nextStatus,
          },
          method: "PATCH",
          token,
        }
      );
      setChangeSet(nextChangeSet);
      setPayloads(payloadText(nextChangeSet));
      setOperationReviewNotes(operationReviewNoteText(nextChangeSet));
      setFlash("Graph draft operation updated.");
    } catch (err) {
      setFlash("", err.message || "Failed to update graph draft operation.");
    } finally {
      endCommand(commandKey);
      setBusy(false);
    }
  }

  async function acceptAll() {
    if (!changeSet || !isCurrent) {
      return;
    }
    // One atomic server request replaces the old per-operation client loop: no
    // partial-failure window and no per-iteration flash clobbering. Invalid
    // proposals are left "proposed" and reported so the user can fix them.
    if (!beginCommand("acceptAll")) {
      return;
    }
    setBusy(true);
    setFlash("", "");
    try {
      const nextChangeSet = await apiRequest(`/graph-drafts/${changeSetId}/accept-all`, {
        method: "POST",
        token,
      });
      setChangeSet(nextChangeSet);
      setPayloads(payloadText(nextChangeSet));
      setOperationReviewNotes(operationReviewNoteText(nextChangeSet));
      const remaining = (nextChangeSet.operations || []).filter(
        (operation) => operation.status === "proposed"
      ).length;
      setFlash(
        remaining > 0
          ? `Accepted the valid proposals; ${remaining} could not be accepted and remain for editing.`
          : "All proposals accepted."
      );
    } catch (err) {
      setFlash("", err.message || "Failed to accept all proposals.");
    } finally {
      endCommand("acceptAll");
      setBusy(false);
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
    if (!beginCommand("commit")) {
      return;
    }
    setBusy(true);
    setFlash("", "");
    try {
      const nextChangeSet = await apiRequest(`/graph-drafts/${changeSetId}/commit`, {
        body: { message: commitMessage.trim() },
        method: "POST",
        token,
      });
      setChangeSet(nextChangeSet);
      setPayloads(payloadText(nextChangeSet));
      setOperationReviewNotes(operationReviewNoteText(nextChangeSet));
      setFlash("Graph draft committed.");
    } catch (err) {
      setFlash("", err.message || "Failed to commit graph draft.");
    } finally {
      endCommand("commit");
      setBusy(false);
    }
  }

  async function submitDraft() {
    if (!changeSet || !canSubmitDraft) {
      return;
    }
    if (!beginCommand("submit")) {
      return;
    }
    setBusy(true);
    setFlash("", "");
    try {
      const nextChangeSet = await apiRequest(`/graph-drafts/${changeSetId}/submit`, {
        method: "POST",
        token,
      });
      setChangeSet(nextChangeSet);
      setFlash("Graph draft submitted for review.");
    } catch (err) {
      setFlash("", err.message || "Failed to submit graph draft.");
    } finally {
      endCommand("submit");
      setBusy(false);
    }
  }

  async function reviewDraft(status) {
    if (!changeSet || !canReviewDraft) {
      return;
    }
    if (!beginCommand("review")) {
      return;
    }
    setBusy(true);
    setFlash("", "");
    try {
      const nextChangeSet = await apiRequest(`/graph-drafts/${changeSetId}/review`, {
        body: { status, note: reviewNote.trim() || null },
        method: "POST",
        token,
      });
      setChangeSet(nextChangeSet);
      setFlash(status === "rejected" ? "Graph draft rejected." : "Changes requested.");
    } catch (err) {
      setFlash("", err.message || "Failed to review graph draft.");
    } finally {
      endCommand("review");
      setBusy(false);
    }
  }

  // Called with the dictation console's current inputs. Returns true only when
  // the server accepted the revision, so the caller can clear those inputs.
  async function reviseDraft({ isRecording, feedback, audioFile, attachments }) {
    if (!changeSet || !canEditDraft) {
      return false;
    }
    if (isRecording) {
      setFlash("", "Stop the recording before revising.");
      return false;
    }
    const trimmedFeedback = String(feedback || "").trim();
    if (!trimmedFeedback && !audioFile && attachments.length === 0) {
      setFlash("", "Add feedback, a voice note, or a file for the AI to revise the draft.");
      return false;
    }
    if (!beginCommand("revise")) {
      return false;
    }
    setBusy(true);
    setFlash("", "");
    try {
      const formData = new FormData();
      if (trimmedFeedback) {
        formData.append("feedback", trimmedFeedback);
      }
      if (audioFile) {
        formData.append("audio", audioFile, audioFile.name);
      }
      attachments.forEach((file) => {
        formData.append("attachments", file, file.name);
      });
      const nextChangeSet = await apiRequest(`/graph-drafts/${changeSetId}/revise`, {
        body: formData,
        method: "POST",
        token,
      });
      setChangeSet(nextChangeSet);
      setPayloads(payloadText(nextChangeSet));
      setOperationReviewNotes(operationReviewNoteText(nextChangeSet));
      setFlash("Draft revised from your feedback.");
      return true;
    } catch (err) {
      setFlash("", err.message || "Failed to revise graph draft.");
      return false;
    } finally {
      endCommand("revise");
      setBusy(false);
    }
  }

  return {
    changeSet,
    payloads,
    operationReviewNotes,
    loading,
    error,
    commitMessage,
    setCommitMessage,
    reviewNote,
    setReviewNote,
    pendingCommands,
    acceptedCount,
    spokenReview,
    canEditDraft,
    canSubmitDraft,
    canReviewDraft,
    canCommitDraft,
    updatePayloadText,
    updateOperationReviewNote,
    patchOperationPayload,
    saveOperation,
    acceptAll,
    commitDraft,
    submitDraft,
    reviewDraft,
    reviseDraft,
  };
}

export { useGraphDraftWorkflow };
