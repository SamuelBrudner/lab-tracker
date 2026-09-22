import { apiRequest } from "../shared/api.js";
import { flashAfterRefresh } from "./flashAfterRefresh.js";

function useQuestionActions({
  token,
  canWrite,
  selectedProjectId,
  refreshProjectData,
  setBusy,
  setFlash,
  questionText,
  setQuestionText,
  questionType,
  questionHypothesis,
  setQuestionHypothesis,
  questionParentIds = [],
  setQuestionParentIds = () => {},
}) {
  async function handleCreateQuestion(event) {
    event.preventDefault();
    if (!selectedProjectId || !canWrite) {
      return;
    }
    if (!questionText.trim()) {
      setFlash("", "Question text is required.");
      return;
    }

    setBusy(true);
    setFlash("", "");
    try {
      try {
        await apiRequest("/questions", {
          body: {
            hypothesis: questionHypothesis.trim() || null,
            project_id: selectedProjectId,
            question_type: questionType,
            parent_question_ids: questionParentIds,
            text: questionText.trim(),
          },
          method: "POST",
          token,
        });
      } catch (err) {
        setFlash("", err.message || "Failed to create question.");
        return;
      }
      setQuestionText("");
      setQuestionHypothesis("");
      setQuestionParentIds([]);
      await flashAfterRefresh({
        refresh: () => refreshProjectData(selectedProjectId),
        setFlash,
        success: "Question staged.",
      });
    } finally {
      setBusy(false);
    }
  }

  async function handleActivateQuestion(questionId) {
    if (!canWrite) {
      return;
    }
    setBusy(true);
    setFlash("", "");
    try {
      try {
        await apiRequest(`/questions/${questionId}`, {
          body: { status: "active" },
          method: "PATCH",
          token,
        });
      } catch (err) {
        setFlash("", err.message || "Failed to activate question.");
        return;
      }
      await flashAfterRefresh({
        refresh: () => refreshProjectData(selectedProjectId),
        setFlash,
        success: "Question activated.",
      });
    } finally {
      setBusy(false);
    }
  }

  return {
    handleActivateQuestion,
    handleCreateQuestion,
  };
}

export { useQuestionActions };
