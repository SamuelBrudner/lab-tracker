import { apiRequest } from "../shared/api.js";

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
      await apiRequest("/questions", {
        body: {
          hypothesis: questionHypothesis.trim() || null,
          project_id: selectedProjectId,
          question_type: questionType,
          text: questionText.trim(),
        },
        method: "POST",
        token,
      });
      setQuestionText("");
      setQuestionHypothesis("");
      await refreshProjectData(selectedProjectId);
      setFlash("Question staged.");
    } catch (err) {
      setFlash("", err.message || "Failed to create question.");
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
      await apiRequest(`/questions/${questionId}`, {
        body: { status: "active" },
        method: "PATCH",
        token,
      });
      await refreshProjectData(selectedProjectId);
      setFlash("Question activated.");
    } catch (err) {
      setFlash("", err.message || "Failed to activate question.");
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
