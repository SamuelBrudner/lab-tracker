import * as React from "react";

import { apiRequest } from "../shared/api.js";

const { useEffect, useState } = React;

function useQuestionExtractionWorkflow({
  token,
  canWrite,
  selectedProjectId,
  refreshProjectData,
  setBusy,
  setFlash,
}) {
  const [extractionNoteId, setExtractionNoteId] = useState("");
  const [extractionNote, setExtractionNote] = useState(null);
  const [extractionNoteRaw, setExtractionNoteRaw] = useState(null);
  const [extractionNoteRawError, setExtractionNoteRawError] = useState("");
  const [extractionCandidates, setExtractionCandidates] = useState([]);

  useEffect(() => {
    setExtractionNoteId("");
    setExtractionNote(null);
    setExtractionNoteRaw(null);
    setExtractionNoteRawError("");
    setExtractionCandidates([]);
  }, [selectedProjectId, token]);

  function handleExtractionNoteIdChange(event) {
    const nextValue = event?.target?.value || "";
    setExtractionNoteId(nextValue);
    setExtractionNote(null);
    setExtractionNoteRaw(null);
    setExtractionNoteRawError("");
    setExtractionCandidates([]);
  }

  async function handleExtractQuestionCandidates(event) {
    event.preventDefault();
    if (!token || !canWrite || !selectedProjectId || !extractionNoteId) {
      return;
    }

    setBusy(true);
    setFlash("", "");
    try {
      const loadedNote = await apiRequest(`/notes/${extractionNoteId}`, { token });
      setExtractionNote(loadedNote);

      if (loadedNote.raw_asset) {
        try {
          const rawPayload = await apiRequest(`/notes/${extractionNoteId}/raw`, { token });
          setExtractionNoteRaw(rawPayload);
          setExtractionNoteRawError("");
        } catch (err) {
          setExtractionNoteRaw(null);
          setExtractionNoteRawError(err.message || "Failed to load raw preview.");
        }
      } else {
        setExtractionNoteRaw(null);
        setExtractionNoteRawError("");
      }

      const payload = await apiRequest(`/notes/${extractionNoteId}/extract-questions`, {
        body: {},
        method: "POST",
        token,
      });
      const extracted = Array.isArray(payload) ? payload : [];
      const batchId = Date.now().toString(36);
      setExtractionCandidates(
        extracted.map((item, index) => ({
          confidence: typeof item.confidence === "number" ? item.confidence : null,
          error: "",
          hypothesis: "",
          local_id: `${batchId}-${index}`,
          parent_question_ids: [],
          provenance: item.provenance || "",
          question_type: item.suggested_question_type || "other",
          selected: true,
          staged_question_id: "",
          status: "pending",
          text: String(item.text || ""),
        }))
      );
      setFlash(
        extracted.length === 0
          ? "No question candidates found for that note."
          : `Loaded ${extracted.length} question candidate(s).`
      );
    } catch (err) {
      setExtractionCandidates([]);
      setFlash("", err.message || "Failed to extract question candidates.");
    } finally {
      setBusy(false);
    }
  }

  function handleUpdateExtractionCandidate(localId, updates) {
    setExtractionCandidates((current) =>
      current.map((item) => (item.local_id === localId ? { ...item, ...updates } : item))
    );
  }

  function handleToggleExtractionCandidateSelected(localId) {
    setExtractionCandidates((current) =>
      current.map((item) =>
        item.local_id === localId ? { ...item, selected: !item.selected } : item
      )
    );
  }

  function handleSelectAllPendingCandidates() {
    setExtractionCandidates((current) =>
      current.map((item) => (item.status === "pending" ? { ...item, selected: true } : item))
    );
  }

  function handleClearCandidateSelection() {
    setExtractionCandidates((current) => current.map((item) => ({ ...item, selected: false })));
  }

  function handleRejectExtractionCandidates(candidateIds) {
    const resolvedIds = Array.isArray(candidateIds)
      ? candidateIds
      : extractionCandidates
          .filter((item) => item.selected && item.status === "pending")
          .map((item) => item.local_id);
    if (resolvedIds.length === 0) {
      return;
    }

    const rejectSet = new Set(resolvedIds);
    setExtractionCandidates((current) =>
      current.map((item) => {
        if (!rejectSet.has(item.local_id) || item.status !== "pending") {
          return item;
        }
        return { ...item, error: "", selected: false, status: "rejected" };
      })
    );
  }

  async function handleStageExtractionCandidates(candidateIds) {
    if (!token || !canWrite || !selectedProjectId || !extractionNote) {
      return;
    }

    const resolvedIds = Array.isArray(candidateIds)
      ? candidateIds
      : extractionCandidates
          .filter((item) => item.selected && item.status === "pending")
          .map((item) => item.local_id);
    if (resolvedIds.length === 0) {
      setFlash("", "Select at least one pending candidate to stage.");
      return;
    }

    const toStage = extractionCandidates.filter(
      (item) => resolvedIds.includes(item.local_id) && item.status === "pending"
    );
    if (toStage.length === 0) {
      setFlash("", "No pending candidates selected.");
      return;
    }

    const emptyTextIds = toStage
      .filter((item) => !String(item.text || "").trim())
      .map((item) => item.local_id);
    if (emptyTextIds.length > 0) {
      const emptySet = new Set(emptyTextIds);
      setExtractionCandidates((current) =>
        current.map((item) =>
          emptySet.has(item.local_id)
            ? { ...item, error: "Question text is required.", status: "error" }
            : item
        )
      );
      setFlash("", "One or more candidates are missing question text.");
      return;
    }

    setBusy(true);
    setFlash("", "");
    const updatesById = new Map();
    const createdQuestionIds = [];

    try {
      const results = await Promise.allSettled(
        toStage.map((candidate) =>
          apiRequest("/questions", {
            body: {
              created_from: "meeting_capture",
              hypothesis: candidate.hypothesis.trim() || null,
              parent_question_ids:
                candidate.parent_question_ids && candidate.parent_question_ids.length > 0
                  ? candidate.parent_question_ids
                  : null,
              project_id: selectedProjectId,
              question_type: candidate.question_type || "other",
              source_provenance: candidate.provenance || null,
              status: "staged",
              text: String(candidate.text || "").trim(),
            },
            method: "POST",
            token,
          })
        )
      );

      results.forEach((result, index) => {
        const candidate = toStage[index];
        if (result.status === "fulfilled" && result.value) {
          const created = result.value;
          createdQuestionIds.push(created.question_id);
          updatesById.set(candidate.local_id, {
            error: "",
            selected: false,
            staged_question_id: created.question_id,
            status: "staged",
          });
          return;
        }
        const stageError =
          result.status === "rejected"
            ? result.reason?.message || String(result.reason || "Failed to stage candidate.")
            : "Failed to stage candidate.";
        updatesById.set(candidate.local_id, {
          error: stageError,
          status: "error",
        });
      });

      setExtractionCandidates((current) =>
        current.map((item) =>
          updatesById.has(item.local_id) ? { ...item, ...updatesById.get(item.local_id) } : item
        )
      );

      if (createdQuestionIds.length > 0) {
        const existingTargets = Array.isArray(extractionNote.targets) ? extractionNote.targets : [];
        const nextTargets = [...existingTargets];
        for (const questionId of createdQuestionIds) {
          if (
            nextTargets.some(
              (target) => target.entity_type === "question" && target.entity_id === questionId
            )
          ) {
            continue;
          }
          nextTargets.push({ entity_id: questionId, entity_type: "question" });
        }
        if (nextTargets.length !== existingTargets.length) {
          const updatedNote = await apiRequest(`/notes/${extractionNote.note_id}`, {
            body: { targets: nextTargets },
            method: "PATCH",
            token,
          });
          setExtractionNote(updatedNote);
        }
      }

      await refreshProjectData(selectedProjectId);
      setFlash(
        createdQuestionIds.length === 1
          ? "Staged 1 question from candidates."
          : `Staged ${createdQuestionIds.length} questions from candidates.`
      );
    } catch (err) {
      setFlash("", err.message || "Failed to stage candidates.");
    } finally {
      setBusy(false);
    }
  }

  return {
    extractionCandidates,
    extractionNote,
    extractionNoteId,
    extractionNoteRaw,
    extractionNoteRawError,
    handleClearCandidateSelection,
    handleExtractQuestionCandidates,
    handleExtractionNoteIdChange,
    handleRejectExtractionCandidates,
    handleSelectAllPendingCandidates,
    handleStageExtractionCandidates,
    handleToggleExtractionCandidateSelected,
    handleUpdateExtractionCandidate,
  };
}

export { useQuestionExtractionWorkflow };
