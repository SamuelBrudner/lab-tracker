import * as React from "react";

import { apiListRequest, apiRequest, buildApiPath } from "../shared/api.js";
import { formatDate } from "../shared/formatters.js";

const { useCallback, useEffect, useMemo, useRef, useState } = React;

const AUDIO_MIME_CANDIDATES = ["audio/webm", "audio/mp4", "audio/ogg"];

function pickAudioMimeType() {
  if (typeof MediaRecorder === "undefined" || typeof MediaRecorder.isTypeSupported !== "function") {
    return "";
  }
  return AUDIO_MIME_CANDIDATES.find((type) => MediaRecorder.isTypeSupported(type)) || "";
}

function audioExtensionForMime(mimeType) {
  if (mimeType.includes("mp4")) {
    return "m4a";
  }
  if (mimeType.includes("ogg")) {
    return "ogg";
  }
  return "webm";
}

function canRecordAudio() {
  return (
    typeof navigator !== "undefined" &&
    Boolean(navigator.mediaDevices?.getUserMedia) &&
    typeof MediaRecorder !== "undefined"
  );
}

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

function normalizeSpeechText(value) {
  return String(value || "")
    .replace(/[`*_#>]/g, "")
    .replace(/\s+/g, " ")
    .trim();
}

function operationProposalText(operation, payloadTextById = {}) {
  let payload = operation?.payload || {};
  const editedPayload = payloadTextById[operation?.operation_id];
  if (typeof editedPayload === "string") {
    try {
      const parsed = JSON.parse(editedPayload);
      if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
        payload = parsed;
      }
    } catch {
      // Narrate the last valid server payload while an edit is incomplete.
    }
  }
  return normalizeSpeechText(
    payload.text ||
      payload.raw_content ||
      payload.label ||
      payload.prompt ||
      payload.statement ||
      operationTitle(operation)
  );
}

function spokenReviewScript(changeSet, payloadTextById = {}) {
  if (!changeSet) {
    return "";
  }
  const operations = changeSet.operations || [];
  const sections = [];
  const summary = normalizeSpeechText(changeSet.summary);
  if (summary) {
    sections.push(`Review summary. ${summary}`);
  }
  sections.push(
    operations.length === 1
      ? "There is 1 proposal."
      : `There are ${operations.length} proposals.`
  );
  operations.forEach((operation, index) => {
    const proposal = [
      `Proposal ${index + 1}.`,
      `${normalizeSpeechText(operationIntent(operation))}.`,
      operationProposalText(operation, payloadTextById),
    ];
    const rationale = normalizeSpeechText(operation.rationale);
    if (rationale) {
      proposal.push(`Model inference. ${rationale}`);
    }
    if (operation.confidence !== null && operation.confidence !== undefined) {
      proposal.push(`${Math.round(operation.confidence * 100)} percent confidence.`);
    }
    sections.push(proposal.join(" "));
  });
  const uncertainties = [
    ...(changeSet.clarification_requests || []),
    ...(changeSet.uncertain_fields || []),
  ]
    .map(normalizeSpeechText)
    .filter(Boolean);
  if (uncertainties.length) {
    sections.push(`Questions for you. ${uncertainties.join(". ")}.`);
  }
  sections.push("You can dictate feedback now, or review each proposal below.");
  return sections.join(" ");
}

function canSpeakReview() {
  const speech = typeof window !== "undefined" ? window.speechSynthesis : null;
  return (
    Boolean(speech) &&
    typeof SpeechSynthesisUtterance !== "undefined" &&
    ["cancel", "pause", "resume", "speak"].every(
      (method) => typeof speech[method] === "function"
    )
  );
}

function payloadText(changeSet) {
  const entries = {};
  for (const operation of changeSet?.operations || []) {
    entries[operation.operation_id] = JSON.stringify(operation.payload || {}, null, 2);
  }
  return entries;
}

function operationReviewNoteText(changeSet) {
  const entries = {};
  for (const operation of changeSet?.operations || []) {
    entries[operation.operation_id] = operation.review_note || "";
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

function canContributeWithRole(user, role) {
  return (
    user?.role === "admin" ||
    role === "contributor" ||
    role === "owner"
  );
}

function canManageWithRole(user, role) {
  return user?.role === "admin" || role === "owner";
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
  const batchProjects = Array.isArray(context.projects) ? context.projects : [];
  const batchValues = (field) =>
    batchProjects.flatMap((project) => (Array.isArray(project[field]) ? project[field] : []));
  if (entityType === "question") {
    return context.active_or_staged_questions || batchValues("active_or_staged_questions");
  }
  if (entityType === "session") {
    return context.recent_sessions || batchValues("recent_sessions");
  }
  if (entityType === "dataset") {
    return context.recent_datasets || batchValues("recent_datasets");
  }
  if (entityType === "analysis") {
    return context.recent_analyses || batchValues("recent_analyses");
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
  user = null,
  setBusy,
  setFlash,
  backPath = "/app",
}) {
  const [changeSet, setChangeSet] = useState(null);
  const [payloads, setPayloads] = useState({});
  const [operationReviewNotes, setOperationReviewNotes] = useState({});
  const [draftProjectRole, setDraftProjectRole] = useState("");
  const [draftProjectId, setDraftProjectId] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [sourceImage, setSourceImage] = useState("");
  const [commitMessage, setCommitMessage] = useState("");
  const [reviewNote, setReviewNote] = useState("");
  const [reviseFeedback, setReviseFeedback] = useState("");
  const [reviseAttachments, setReviseAttachments] = useState([]);
  const [reviseAudio, setReviseAudio] = useState(null);
  const [isRecording, setIsRecording] = useState(false);
  const [speechStatus, setSpeechStatus] = useState("idle");
  const mediaRecorderRef = useRef(null);
  const audioChunksRef = useRef([]);
  const audioStreamRef = useRef(null);
  const startingRef = useRef(false);
  const speechUtteranceRef = useRef(null);
  const mountedRef = useRef(true);

  const acceptedCount = useMemo(
    () =>
      (changeSet?.operations || []).filter((operation) => operation.status === "accepted")
        .length,
    [changeSet]
  );
  const visibleSourceRegions = useMemo(() => sourceRegions(changeSet), [changeSet]);
  const spokenReview = useMemo(
    () => spokenReviewScript(changeSet, payloads),
    [changeSet, payloads]
  );
  const isAdmin = user?.role === "admin";
  const usesDraftProjectAccess = Boolean(changeSet?.project_id);
  const hasDraftProjectAccess = usesDraftProjectAccess && draftProjectId === changeSet.project_id;
  const effectiveCanWrite = usesDraftProjectAccess
    ? isAdmin || (hasDraftProjectAccess && canContributeWithRole(user, draftProjectRole))
    : Boolean(canWrite);
  const effectiveCanManageGraph = usesDraftProjectAccess
    ? isAdmin || (hasDraftProjectAccess && canManageWithRole(user, draftProjectRole))
    : Boolean(canManageGraph);
  const canEditDraft =
    effectiveCanWrite && ["ready", "changes_requested"].includes(changeSet?.status || "");
  const recordingSupported = canRecordAudio();
  const canSubmitDraft =
    effectiveCanWrite && ["ready", "changes_requested"].includes(changeSet?.status || "");
  const canReviewDraft = effectiveCanManageGraph && changeSet?.status === "submitted";
  const canCommitDraft =
    effectiveCanManageGraph && ["ready", "submitted"].includes(changeSet?.status || "");
  const speechSupported = canSpeakReview();

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
      setOperationReviewNotes(operationReviewNoteText(nextChangeSet));
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

  const stopSpeech = useCallback((updateStatus = true) => {
    const utterance = speechUtteranceRef.current;
    if (utterance) {
      utterance.onend = null;
      utterance.onerror = null;
    }
    if (canSpeakReview()) {
      window.speechSynthesis.cancel();
    }
    speechUtteranceRef.current = null;
    if (updateStatus) {
      setSpeechStatus("idle");
    }
  }, []);

  useEffect(() => () => stopSpeech(false), [stopSpeech]);

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

  function toggleSpeech() {
    if (!speechSupported || !spokenReview) {
      return;
    }
    if (speechStatus === "speaking") {
      window.speechSynthesis.pause();
      setSpeechStatus("paused");
      return;
    }
    if (speechStatus === "paused") {
      window.speechSynthesis.resume();
      setSpeechStatus("speaking");
      return;
    }

    window.speechSynthesis.cancel();
    const utterance = new SpeechSynthesisUtterance(spokenReview);
    utterance.lang = "en-US";
    utterance.rate = 0.95;
    utterance.onend = () => {
      if (speechUtteranceRef.current === utterance) {
        speechUtteranceRef.current = null;
        setSpeechStatus("idle");
      }
    };
    utterance.onerror = () => {
      if (speechUtteranceRef.current === utterance) {
        speechUtteranceRef.current = null;
        setSpeechStatus("idle");
      }
    };
    speechUtteranceRef.current = utterance;
    window.speechSynthesis.speak(utterance);
    setSpeechStatus("speaking");
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
    setBusy(true);
    setFlash("", "");
    try {
      const nextChangeSet = await apiRequest(
        `/graph-drafts/${changeSet.change_set_id}/operations/${operation.operation_id}`,
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
      setOperationReviewNotes(operationReviewNoteText(nextChangeSet));
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

  const stopAudioStream = useCallback(() => {
    const stream = audioStreamRef.current;
    if (stream) {
      stream.getTracks().forEach((track) => track.stop());
      audioStreamRef.current = null;
    }
  }, []);

  useEffect(
    () => () => {
      if (reviseAudio?.url) {
        URL.revokeObjectURL(reviseAudio.url);
      }
    },
    [reviseAudio]
  );

  useEffect(
    () => () => {
      mountedRef.current = false;
      const recorder = mediaRecorderRef.current;
      if (recorder && recorder.state !== "inactive") {
        recorder.stop();
      }
      mediaRecorderRef.current = null;
      stopAudioStream();
    },
    [stopAudioStream]
  );

  function handleAttachmentChange(event) {
    const files = Array.from(event.target.files || []);
    if (files.length) {
      setReviseAttachments((current) => [...current, ...files]);
    }
    // Reset so selecting the same file again still fires onChange.
    event.target.value = "";
  }

  function removeAttachment(index) {
    setReviseAttachments((current) => current.filter((_, position) => position !== index));
  }

  function clearReviseAudio() {
    setReviseAudio((current) => {
      if (current?.url) {
        URL.revokeObjectURL(current.url);
      }
      return null;
    });
  }

  async function startRecording() {
    // getUserMedia stays pending while the permission prompt is open, and
    // isRecording only flips true once it resolves. Guard the in-flight window
    // so a second click can't start a second stream and orphan the first one
    // (which would leak a live microphone).
    if (startingRef.current || mediaRecorderRef.current) {
      return;
    }
    if (!canRecordAudio()) {
      setFlash("", "This browser does not support microphone recording.");
      return;
    }
    startingRef.current = true;
    stopSpeech();
    setFlash("", "");
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      if (!mountedRef.current) {
        stream.getTracks().forEach((track) => track.stop());
        return;
      }
      audioStreamRef.current = stream;
      const mimeType = pickAudioMimeType();
      const recorder = new MediaRecorder(stream, mimeType ? { mimeType } : undefined);
      audioChunksRef.current = [];
      recorder.addEventListener("dataavailable", (event) => {
        if (event.data && event.data.size > 0) {
          audioChunksRef.current.push(event.data);
        }
      });
      recorder.addEventListener("stop", () => {
        stopAudioStream();
        if (!mountedRef.current) {
          audioChunksRef.current = [];
          return;
        }
        const type = recorder.mimeType || mimeType || "audio/webm";
        const blob = new Blob(audioChunksRef.current, { type });
        audioChunksRef.current = [];
        if (blob.size > 0) {
          const file = new File([blob], `dictated-feedback.${audioExtensionForMime(type)}`, {
            type,
          });
          setReviseAudio((current) => {
            if (current?.url) {
              URL.revokeObjectURL(current.url);
            }
            return { file, url: URL.createObjectURL(blob) };
          });
        }
        setIsRecording(false);
      });
      mediaRecorderRef.current = recorder;
      recorder.start();
      setIsRecording(true);
    } catch {
      stopAudioStream();
      if (mountedRef.current) {
        setIsRecording(false);
        setFlash("", "Could not access the microphone. Check browser permissions.");
      }
    } finally {
      startingRef.current = false;
    }
  }

  const stopRecording = useCallback(() => {
    const recorder = mediaRecorderRef.current;
    if (recorder && recorder.state !== "inactive") {
      recorder.stop();
    }
    mediaRecorderRef.current = null;
  }, []);

  // If the draft leaves the editable state mid-recording (e.g. its status
  // changes from an action elsewhere), the Stop control is gated on
  // canEditDraft and would strand a live mic — so auto-stop to release it.
  useEffect(() => {
    if (isRecording && !canEditDraft) {
      stopRecording();
    }
  }, [isRecording, canEditDraft, stopRecording]);

  function toggleRecording() {
    if (isRecording) {
      stopRecording();
    } else {
      startRecording();
    }
  }

  async function reviseDraft() {
    if (!changeSet || !canEditDraft) {
      return;
    }
    if (isRecording) {
      setFlash("", "Stop the recording before revising.");
      return;
    }
    const feedback = reviseFeedback.trim();
    if (!feedback && !reviseAudio && reviseAttachments.length === 0) {
      setFlash("", "Add feedback, a voice note, or a file for the AI to revise the draft.");
      return;
    }
    setBusy(true);
    setFlash("", "");
    try {
      const formData = new FormData();
      if (feedback) {
        formData.append("feedback", feedback);
      }
      if (reviseAudio?.file) {
        formData.append("audio", reviseAudio.file, reviseAudio.file.name);
      }
      reviseAttachments.forEach((file) => {
        formData.append("attachments", file, file.name);
      });
      const nextChangeSet = await apiRequest(`/graph-drafts/${changeSet.change_set_id}/revise`, {
        body: formData,
        method: "POST",
        token,
      });
      setChangeSet(nextChangeSet);
      setPayloads(payloadText(nextChangeSet));
      setOperationReviewNotes(operationReviewNoteText(nextChangeSet));
      setReviseFeedback("");
      setReviseAttachments([]);
      clearReviseAudio();
      setFlash("Draft revised from your feedback.");
    } catch (err) {
      setFlash("", err.message || "Failed to revise graph draft.");
    } finally {
      setBusy(false);
    }
  }

  function renderAudioReviewConsole() {
    const speechButtonLabel =
      speechStatus === "speaking"
        ? "Pause audio review"
        : speechStatus === "paused"
          ? "Resume audio review"
          : "Listen to review";
    return (
      <section className="ai-revise audio-review-console" aria-labelledby="audio-review-title">
        <div className="audio-review-heading">
          <div>
            <h3 id="audio-review-title">Listen &amp; respond</h3>
            <p className="subtle">
              Hear the summary and proposals, then speak or type corrections for the AI.
            </p>
          </div>
          {speechStatus !== "idle" ? (
            <span className="pill audio-review-status" role="status">
              {speechStatus === "paused" ? "Audio paused" : "Reading review"}
            </span>
          ) : null}
        </div>

        <div className="audio-review-primary-actions">
          {speechSupported ? (
            <button
              type="button"
              className="btn-primary"
              disabled={!spokenReview}
              onClick={toggleSpeech}
              aria-pressed={speechStatus === "speaking"}
            >
              {speechButtonLabel}
            </button>
          ) : (
            <p className="subtle audio-review-unavailable">
              Spoken playback is unavailable in this browser; the review remains below.
            </p>
          )}
          {speechStatus !== "idle" ? (
            <button type="button" className="btn-secondary" onClick={() => stopSpeech()}>
              Stop audio
            </button>
          ) : null}
          <button
            type="button"
            className={`btn-secondary${isRecording ? " recording" : ""}`}
            disabled={isRecording ? false : !canEditDraft || !recordingSupported}
            title={
              recordingSupported
                ? undefined
                : "Microphone recording isn't supported in this browser."
            }
            onClick={toggleRecording}
            aria-pressed={isRecording}
          >
            {isRecording ? "Stop recording" : "Dictate feedback"}
          </button>
        </div>

        {isRecording ? (
          <p className="audio-recording-status" role="status">
            Recording feedback… tap Stop recording when you&apos;re finished.
          </p>
        ) : null}
        {reviseAudio ? (
          <div className="ai-revise-attachment audio-review-recording">
            <audio
              aria-label="Recorded feedback preview"
              controls
              src={reviseAudio.url}
              className="ai-revise-audio"
            />
            <button
              type="button"
              className="btn-link"
              onClick={clearReviseAudio}
              disabled={!canEditDraft}
            >
              Remove voice note
            </button>
          </div>
        ) : null}

        <details className="context-details audio-review-more">
          <summary>Type feedback or attach an image</summary>
          <div className="audio-review-more-body">
            <textarea
              className="ai-revise-input"
              rows={2}
              placeholder="Tell the AI how to revise these proposals — e.g. 'drop the dataset link; the claim isn't supported yet, make it a clarification instead'. You can also dictate feedback or attach an image."
              value={reviseFeedback}
              disabled={!canEditDraft || isRecording}
              onChange={(event) => setReviseFeedback(event.target.value)}
            />
            <label
              className={`btn-secondary ai-revise-attach${canEditDraft ? "" : " disabled"}`}
            >
              Attach image
              <input
                type="file"
                accept="image/*"
                multiple
                className="sr-only"
                disabled={!canEditDraft}
                onChange={handleAttachmentChange}
              />
            </label>
            {reviseAttachments.length ? (
              <ul className="ai-revise-files">
                {reviseAttachments.map((file, index) => (
                  <li key={`${file.name}-${index}`} className="ai-revise-attachment">
                    <span className="ai-revise-file-name">{file.name}</span>
                    <button
                      type="button"
                      className="btn-link"
                      onClick={() => removeAttachment(index)}
                      disabled={!canEditDraft}
                    >
                      Remove
                    </button>
                  </li>
                ))}
              </ul>
            ) : null}
          </div>
        </details>
        <button
          type="button"
          className="btn-primary audio-review-submit"
          disabled={
            !canEditDraft ||
            isRecording ||
            (!reviseFeedback.trim() && !reviseAudio && reviseAttachments.length === 0)
          }
          onClick={reviseDraft}
        >
          Revise with AI
        </button>
      </section>
    );
  }

  return (
    <article className="card span-12">
      <div className="item-head">
        <div className="review-title-row">
          <button type="button" className="btn-link" onClick={() => navigate(backPath)}>
            Back
          </button>
          <h2>Review</h2>
        </div>
        {loading ? <span className="pill">Loading...</span> : null}
      </div>
      {error ? <p className="flash error">{error}</p> : null}

      {changeSet ? (
        <div className="daily-review-report">
          {changeSet.summary ? (
            <div className="review-summary">
              {String(changeSet.summary)
                .split(/\n{2,}/)
                .map((para) => para.trim())
                .filter(Boolean)
                .map((para, index) => (
                  <p key={index}>{para}</p>
                ))}
            </div>
          ) : null}
          <p className="review-lead subtle">
            {changeSet.source_note_count || (changeSet.source_note_ids || []).length || 1}{" "}
            {(changeSet.source_note_count || (changeSet.source_note_ids || []).length || 1) === 1
              ? "capture"
              : "captures"}{" "}
            from this review became{" "}
            {(changeSet.operations || []).length === 1
              ? "1 proposal"
              : `${(changeSet.operations || []).length} proposals`}{" "}
            for your graph. Keep what&apos;s right, then commit — nothing changes until you do.
          </p>
          {renderAudioReviewConsole()}
          {changeSet.error_metadata?.message ? (
            <p className="flash error">{changeSet.error_metadata.message}</p>
          ) : null}

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

          <div className="review-report">
            {(changeSet.operations || []).map((operation) => {
              const parsed =
                parsedPayloadFromText(payloads[operation.operation_id]) || operation.payload || {};
              const proposed =
                parsed.text ||
                parsed.raw_content ||
                parsed.label ||
                parsed.prompt ||
                parsed.statement ||
                "";
              const linkType = semanticLinkTargetType(operation);
              return (
                <div className="review-proposal" key={operation.operation_id}>
                  <div className="review-proposal-body">
                    <p className="review-proposal-intent subtle">{operationIntent(operation)}</p>
                    <p className="review-proposal-text">
                      {proposed || operationTitle(operation)}
                    </p>
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
                          <p
                            className="source-snippet"
                            key={`${operation.operation_id}-${index}`}
                          >
                            {sourceRefText(ref)}
                          </p>
                        ))}
                      </div>
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
                                patchOperationPayload(operation, (payload) =>
                                  nextPayloadWithTarget(payload, linkType, event.target.value)
                                )
                              }
                              value={payloadTargetId(
                                parsedPayloadFromText(payloads[operation.operation_id]) || {},
                                linkType
                              )}
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
                        {Object.prototype.hasOwnProperty.call(
                          operation.payload || {},
                          "raw_content"
                        ) ? (
                          <label>
                            Note text
                            <textarea
                              value={
                                parsedPayloadFromText(payloads[operation.operation_id])
                                  ?.raw_content || ""
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
                        <label>
                          Decision note
                          <textarea
                            value={operationReviewNotes[operation.operation_id] || ""}
                            disabled={!canEditDraft}
                            onChange={(event) =>
                              updateOperationReviewNote(operation.operation_id, event.target.value)
                            }
                          />
                        </label>
                        <div className="inline">
                          <button
                            type="button"
                            className="btn-secondary"
                            disabled={!canEditDraft}
                            onClick={() => saveOperation(operation)}
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
                  </aside>
                </div>
              );
            })}
          </div>

          {(changeSet.uncertain_fields || []).length > 0 ||
          (changeSet.clarification_requests || []).length > 0 ? (
            <div className="review-unsure">
              <div className="subtle">The model wasn&apos;t sure about</div>
              <ul className="compact-list">
                {(changeSet.clarification_requests || []).map((item) => (
                  <li key={item}>{item}</li>
                ))}
                {(changeSet.uncertain_fields || []).map((item) => (
                  <li key={item}>{item}</li>
                ))}
              </ul>
            </div>
          ) : null}

          <div className="review-actions">
            <div className="review-tally">
              <strong>
                {acceptedCount} of {(changeSet.operations || []).length} kept
              </strong>
              <button
                type="button"
                className="btn-secondary"
                disabled={!canEditDraft}
                onClick={acceptAll}
              >
                Accept all
              </button>
            </div>

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
              <button className="btn-primary" disabled={!canCommitDraft || acceptedCount === 0}>
                Commit accepted changes
              </button>
            </form>
          </div>

          <details className="context-details review-meta">
            <summary>Details &amp; provenance</summary>
            <div className="stack">
              <div className="inline">
                <span className={statusClass(changeSet.status)}>{changeSet.status}</span>
                <span className="pill">{changeSet.draft_mode || "graph_context"}</span>
                <span className="pill">{changeSet.model}</span>
                <span className="pill">{changeSet.provider}</span>
              </div>
              <div>
                <div className="subtle">Source note</div>
                <div className="mono">{changeSet.source_note_id}</div>
              </div>
              {(changeSet.source_note_ids || []).length > 1 ? (
                <div>
                  <div className="subtle">Notes in this review</div>
                  <div className="inline">
                    {(changeSet.source_note_ids || []).map((noteId) => (
                      <span className="pill mono" key={noteId}>
                        {noteId}
                      </span>
                    ))}
                  </div>
                </div>
              ) : null}
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
          </details>
        </div>
      ) : null}

      <div className="inline detail-actions">
        <button type="button" className="btn-secondary" onClick={() => navigate(backPath)}>
          Back
        </button>
      </div>
    </article>
  );
}

export { GraphDraftDetailCard, spokenReviewScript };
