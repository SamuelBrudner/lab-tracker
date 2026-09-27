import * as React from "react";

import { apiListRequest, apiRequest, buildApiPath } from "../shared/api.js";
import { graphDrafts, memberOnboarding } from "../shared/gateways/index.js";
import {
  canContributeWithRole,
  canManageWithRole,
  decisionFlashMessage,
  defaultCommitMessage,
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
  // Scope undo to operations changed by this client's most recent bulk action.
  // The server's acceptance_mode remains the authority for whether each one is
  // still an unreviewed bulk acceptance.
  const [bulkAcceptedIds, setBulkAcceptedIds] = useState([]);
  const [draftProjectRole, setDraftProjectRole] = useState("");
  const [draftProjectId, setDraftProjectId] = useState("");
  const [draftAccessError, setDraftAccessError] = useState("");
  const [onboardingAccess, setOnboardingAccess] = useState({
    capabilities: null,
    projectId: "",
  });
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
  const undoableOperationIds = useMemo(() => {
    const stillBulkAccepted = new Set(
      (changeSet?.operations || [])
        .filter(
          (operation) =>
            operation.status === "accepted" && operation.acceptance_mode === "bulk_accepted"
        )
        .map((operation) => operation.operation_id)
    );
    return bulkAcceptedIds.filter((operationId) => stillBulkAccepted.has(operationId));
  }, [bulkAcceptedIds, changeSet]);
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
  const isMemberOnboarding = changeSet?.purpose === "member_checkpoint_alignment";
  const hasOnboardingAccess =
    isMemberOnboarding && onboardingAccess.projectId === changeSet?.project_id;
  const onboardingAuthorId =
    changeSet?.review_assignee_user_id ||
    changeSet?.created_by_user_id ||
    changeSet?.review_assignee ||
    changeSet?.created_by ||
    "";
  const isOnboardingAuthor = Boolean(
    user?.user_id && onboardingAuthorId && user.user_id === onboardingAuthorId
  );
  const canWriteCurrentDraft = isMemberOnboarding
    ? isOnboardingAuthor &&
      (isAdmin || Boolean(hasOnboardingAccess && onboardingAccess.capabilities?.can_align))
    : effectiveCanWrite;
  const canManageCurrentDraft = isMemberOnboarding
    ? isAdmin || Boolean(hasOnboardingAccess && onboardingAccess.capabilities?.can_commit)
    : effectiveCanManageGraph;
  const canEditDraft =
    isCurrent &&
    canWriteCurrentDraft &&
    ["ready", "changes_requested"].includes(changeSet?.status || "");
  const canSubmitDraft =
    isCurrent &&
    canWriteCurrentDraft &&
    ["ready", "changes_requested"].includes(changeSet?.status || "");
  const canReviewDraft =
    isCurrent && canManageCurrentDraft && changeSet?.status === "submitted";
  const canCommitDraft =
    isCurrent &&
    canManageCurrentDraft &&
    (changeSet?.purpose === "member_checkpoint_alignment"
      ? changeSet?.status === "submitted"
      : ["ready", "submitted"].includes(changeSet?.status || ""));
  // Daily Review batches are reviewed per operation; the API refuses to
  // regenerate them, so AI revision is not offered for them at all.
  const supportsAiRevision = changeSet?.draft_mode !== "graph_batch";
  const canReviseDraft = canEditDraft && supportsAiRevision && !isMemberOnboarding;

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
      setBulkAcceptedIds([]);
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
    setDraftAccessError("");
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
      .catch((err) => {
        if (!canceled) {
          // Stay fail-closed (no role) but say why the review actions are off.
          setDraftProjectRole("");
          setDraftAccessError(
            `Could not confirm your access to this project: ${
              err?.message || "membership lookup failed."
            } Review actions stay disabled until it loads.`
          );
        }
      });
    return () => {
      canceled = true;
    };
  }, [changeSet?.project_id, token, user?.role, user?.user_id]);

  useEffect(() => {
    let canceled = false;
    const projectId = changeSet?.project_id || "";
    if (changeSet?.purpose !== "member_checkpoint_alignment" || !projectId) {
      setOnboardingAccess({ capabilities: null, projectId: "" });
      return () => {
        canceled = true;
      };
    }
    setOnboardingAccess({ capabilities: null, projectId });
    memberOnboarding
      .getMemberOnboarding(projectId, { token })
      .then((state) => {
        if (!canceled && state?.project_id === projectId) {
          setOnboardingAccess({
            capabilities: state.capabilities || null,
            projectId,
          });
        }
      })
      .catch(() => {
        if (!canceled) {
          setOnboardingAccess({ capabilities: null, projectId });
        }
      });
    return () => {
      canceled = true;
    };
  }, [changeSet?.project_id, changeSet?.purpose, token]);

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

  // The buffered payload for an operation as a JSON object, or null (with a
  // flash) when the reviewer's text is not one.
  function bufferedPayload(operation) {
    let parsedPayload;
    try {
      parsedPayload = JSON.parse(payloads[operation.operation_id] || "{}");
    } catch {
      setFlash("", "Operation payload must be valid JSON.");
      return null;
    }
    if (!parsedPayload || typeof parsedPayload !== "object" || Array.isArray(parsedPayload)) {
      setFlash("", "Operation payload must be a JSON object.");
      return null;
    }
    return parsedPayload;
  }

  function adoptChangeSet(nextChangeSet) {
    setChangeSet(nextChangeSet);
    setPayloads(payloadText(nextChangeSet));
    setOperationReviewNotes(operationReviewNoteText(nextChangeSet));
  }

  // One PATCH against an operation, guarded per operation so each row is
  // independently non-duplicable and never aimed at a draft that is no
  // longer the active route. Resolves true only when the server accepted it.
  async function patchOperation(operation, body, flashMessage) {
    if (!isCurrent) {
      return false;
    }
    const commandKey = `op:${operation.operation_id}`;
    if (!beginCommand(commandKey)) {
      return false;
    }
    setBusy(true);
    setFlash("", "");
    try {
      const nextChangeSet = await apiRequest(
        `/graph-drafts/${changeSetId}/operations/${operation.operation_id}`,
        { body, method: "PATCH", token }
      );
      adoptChangeSet(nextChangeSet);
      setFlash(flashMessage);
      return true;
    } catch (err) {
      setFlash("", err.message || "Failed to update graph draft operation.");
      return false;
    } finally {
      endCommand(commandKey);
      setBusy(false);
    }
  }

  // `decision` is one of accepted / rejected / proposed when a decision
  // control was used, and undefined for a plain "save my edits".
  async function saveOperation(operation, decision) {
    const parsedPayload = bufferedPayload(operation);
    if (!parsedPayload) {
      return;
    }
    return patchOperation(
      operation,
      {
        payload: parsedPayload,
        review_note: operationReviewNotes[operation.operation_id]?.trim() || null,
        status: decision ?? operation.status,
      },
      // Name the proposal as just saved, not as it read before the edit.
      decisionFlashMessage({ ...operation, payload: parsedPayload }, decision)
    );
  }

  // A rejection carries the reviewer's structured reason on the same PATCH.
  async function rejectOperation(operation, reason) {
    const parsedPayload = bufferedPayload(operation);
    if (!parsedPayload) {
      return;
    }
    return patchOperation(
      operation,
      {
        payload: parsedPayload,
        reject_reason: reason,
        review_note: operationReviewNotes[operation.operation_id]?.trim() || null,
        status: "rejected",
      },
      decisionFlashMessage({ ...operation, payload: parsedPayload }, "rejected")
    );
  }

  // Deferral is its own stamp: the proposal stays proposed, keeps the
  // reviewer's buffered edits untouched, and is skipped by accept-all.
  async function deferOperation(operation) {
    return patchOperation(
      operation,
      { deferred: true },
      decisionFlashMessage(operation, "deferred")
    );
  }

  // Set one of the draft's source captures aside with a named reason. The
  // draft is reloaded afterwards so its source view reflects the archive.
  async function archiveSourceNote(noteId, reason) {
    if (!isCurrent) {
      return false;
    }
    const commandKey = `archive:${noteId}`;
    if (!beginCommand(commandKey)) {
      return false;
    }
    setBusy(true);
    setFlash("", "");
    try {
      await apiRequest(`/notes/${noteId}/archive`, {
        body: { reason },
        method: "POST",
        token,
      });
      await loadDraft();
      setFlash(`Capture set aside (${reason}).`);
      return true;
    } catch (err) {
      setFlash("", err.message || "Failed to set the capture aside.");
      return false;
    } finally {
      endCommand(commandKey);
      setBusy(false);
    }
  }

  // Accept or reject one proposed provenance link; returns the updated link
  // or null when the server refused it.
  async function decideProvenanceLink(linkId, status) {
    const commandKey = `link:${linkId}`;
    if (!beginCommand(commandKey)) {
      return null;
    }
    setBusy(true);
    setFlash("", "");
    try {
      const link = await apiRequest(`/provenance-links/${linkId}`, {
        body: { status },
        method: "PATCH",
        token,
      });
      setFlash(status === "accepted" ? "Provenance link accepted." : "Provenance link rejected.");
      return link;
    } catch (err) {
      setFlash("", err.message || "Failed to decide the provenance link.");
      return null;
    } finally {
      endCommand(commandKey);
      setBusy(false);
    }
  }

  async function acceptAll() {
    if (!changeSet || !canEditDraft) {
      return;
    }
    const dirty = [];
    const invalid = [];
    for (const operation of changeSet.operations || []) {
      if (operation.status !== "proposed") {
        continue;
      }
      const text = payloads[operation.operation_id];
      const stored = JSON.stringify(operation.payload || {}, null, 2);
      const note = (operationReviewNotes[operation.operation_id] || "").trim();
      const noteDirty = note !== (operation.review_note || "").trim();
      const payloadEdited = text !== undefined && text !== stored;
      if (!payloadEdited && !noteDirty) {
        continue;
      }
      let parsed;
      try {
        parsed = JSON.parse(payloadEdited ? text : stored);
      } catch {
        invalid.push(operation);
        continue;
      }
      if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
        invalid.push(operation);
        continue;
      }
      if (!noteDirty && JSON.stringify(parsed) === JSON.stringify(operation.payload || {})) {
        continue;
      }
      dirty.push({ note: note || null, operation, parsed });
    }
    if (invalid.length > 0) {
      setFlash(
        "",
        invalid.length === 1
          ? "One proposal has invalid JSON. Fix or revert it before accepting all."
          : `${invalid.length} proposals have invalid JSON. Fix or revert them before accepting all.`
      );
      return;
    }
    if (!beginCommand("acceptAll")) {
      return;
    }
    setBusy(true);
    setFlash("", "");
    try {
      // Persist buffered payload and decision-note edits while operations are
      // still proposed. The bulk endpoint can then stamp the decision honestly
      // as bulk_accepted without discarding the reviewer's text.
      for (const { note, operation, parsed } of dirty) {
        await apiRequest(
          `/graph-drafts/${changeSetId}/operations/${operation.operation_id}`,
          {
            body: {
              payload: parsed,
              review_note: note,
              status: "proposed",
            },
            method: "PATCH",
            token,
          }
        );
      }
      const proposedBefore = new Set(
        (changeSet.operations || [])
          .filter((operation) => operation.status === "proposed")
          .map((operation) => operation.operation_id)
      );
      const nextChangeSet = await apiRequest(`/graph-drafts/${changeSetId}/accept-all`, {
        method: "POST",
        token,
      });
      setBulkAcceptedIds(
        (nextChangeSet.operations || [])
          .filter(
            (operation) =>
              operation.status === "accepted" &&
              operation.acceptance_mode === "bulk_accepted" &&
              proposedBefore.has(operation.operation_id)
          )
          .map((operation) => operation.operation_id)
      );
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

  async function undoAcceptAll() {
    const targets = undoableOperationIds;
    if (!changeSet || !canEditDraft || targets.length === 0) {
      return;
    }
    if (!beginCommand("undoAcceptAll")) {
      return;
    }
    setBusy(true);
    setFlash("", "");
    let latest = null;
    let reverted = 0;
    try {
      for (const operationId of targets) {
        latest = await apiRequest(
          `/graph-drafts/${changeSetId}/operations/${operationId}`,
          {
            body: { status: "proposed" },
            method: "PATCH",
            token,
          }
        );
        reverted += 1;
      }
      setBulkAcceptedIds([]);
      setFlash(
        targets.length === 1
          ? "Undid the bulk accept. 1 proposal is awaiting your decision again."
          : `Undid the bulk accept. ${targets.length} proposals are awaiting your decision again.`
      );
    } catch (err) {
      setFlash(
        "",
        reverted > 0
          ? `Undo stopped after ${reverted} of ${targets.length} proposals: ${
              err.message || "the request failed."
            }`
          : err.message || "Failed to undo the bulk accept."
      );
    } finally {
      if (latest) {
        setChangeSet(latest);
        setPayloads(payloadText(latest));
        setOperationReviewNotes(operationReviewNoteText(latest));
      }
      endCommand("undoAcceptAll");
      setBusy(false);
    }
  }

  // Shown as the commit field's placeholder and used verbatim when the person
  // leaves it empty, so a typed message is optional rather than a gate.
  const suggestedCommitMessage = defaultCommitMessage(changeSet, acceptedCount);

  async function commitDraft(event) {
    event.preventDefault();
    if (!changeSet || !canCommitDraft) {
      return;
    }
    const message = commitMessage.trim() || suggestedCommitMessage;
    if (!beginCommand("commit")) {
      return;
    }
    setBusy(true);
    setFlash("", "");
    try {
      const nextChangeSet = await apiRequest(`/graph-drafts/${changeSetId}/commit`, {
        body: { message },
        method: "POST",
        token,
      });
      setChangeSet(nextChangeSet);
      setPayloads(payloadText(nextChangeSet));
      setOperationReviewNotes(operationReviewNoteText(nextChangeSet));
      setFlash(
        acceptedCount === 1
          ? "Committed 1 change to the graph."
          : `Committed ${acceptedCount} changes to the graph.`
      );
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
    if (!changeSet || !canReviseDraft) {
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
    accessError: draftAccessError,
    commitMessage,
    setCommitMessage,
    suggestedCommitMessage,
    reviewNote,
    setReviewNote,
    pendingCommands,
    acceptedCount,
    undoableOperationIds,
    spokenReview,
    canEditDraft,
    canReviseDraft,
    supportsAiRevision,
    canSubmitDraft,
    canReviewDraft,
    canCommitDraft,
    canWriteProject: effectiveCanWrite,
    updatePayloadText,
    updateOperationReviewNote,
    patchOperationPayload,
    saveOperation,
    rejectOperation,
    deferOperation,
    archiveSourceNote,
    decideProvenanceLink,
    acceptAll,
    undoAcceptAll,
    commitDraft,
    submitDraft,
    reviewDraft,
    reviseDraft,
  };
}

export { useGraphDraftWorkflow };
