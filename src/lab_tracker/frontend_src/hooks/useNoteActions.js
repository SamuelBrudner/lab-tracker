import { apiRequest } from "../shared/api.js";

function useNoteActions({
  token,
  canWrite,
  selectedProjectId,
  refreshProjectData,
  setBusy,
  setFlash,
  noteText,
  setNoteText,
  uploadFile,
  setUploadFile,
  uploadTranscript,
  setUploadTranscript,
  uploadTargetQuestionId,
  setUploadTargetQuestionId,
}) {
  async function handleCreateTextNote(event) {
    event.preventDefault();
    if (!selectedProjectId || !canWrite) {
      return;
    }
    if (!noteText.trim()) {
      setFlash("", "Note text is required.");
      return;
    }

    setBusy(true);
    setFlash("", "");
    try {
      await apiRequest("/notes", {
        body: {
          project_id: selectedProjectId,
          raw_content: noteText.trim(),
        },
        method: "POST",
        token,
      });
      setNoteText("");
      await refreshProjectData(selectedProjectId);
      setFlash("Text note added.");
    } catch (err) {
      setFlash("", err.message || "Failed to create note.");
    } finally {
      setBusy(false);
    }
  }

  async function handleUploadNote(event) {
    event.preventDefault();
    if (!selectedProjectId || !canWrite) {
      return;
    }
    if (!uploadFile) {
      setFlash("", "Select a file before upload.");
      return;
    }

    setBusy(true);
    setFlash("", "");
    try {
      const payload = new FormData();
      payload.append("file", uploadFile);
      payload.append("project_id", selectedProjectId);
      if (uploadTranscript.trim()) {
        payload.append("transcribed_text", uploadTranscript.trim());
      }
      if (uploadTargetQuestionId) {
        payload.append(
          "targets",
          JSON.stringify([
            {
              entity_id: uploadTargetQuestionId,
              entity_type: "question",
            },
          ])
        );
      }

      await apiRequest("/notes/upload-file", {
        body: payload,
        method: "POST",
        token,
      });
      setUploadFile(null);
      setUploadTranscript("");
      setUploadTargetQuestionId("");
      event.target.reset();
      await refreshProjectData(selectedProjectId);
      setFlash("Note file uploaded.");
    } catch (err) {
      setFlash("", err.message || "Failed to upload note.");
    } finally {
      setBusy(false);
    }
  }

  return {
    handleCreateTextNote,
    handleUploadNote,
  };
}

export { useNoteActions };
