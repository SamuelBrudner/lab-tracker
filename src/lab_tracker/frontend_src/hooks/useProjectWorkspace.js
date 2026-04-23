import { useProjectWorkspaceActions } from "./useProjectWorkspaceActions.js";
import { useProjectWorkspaceData } from "./useProjectWorkspaceData.js";
import { useProjectWorkspaceForms } from "./useProjectWorkspaceForms.js";

function useProjectWorkspace({ token, canWrite, setBusy, setFlash }) {
  const workspaceData = useProjectWorkspaceData({ token, setBusy, setFlash });
  const workspaceForms = useProjectWorkspaceForms({
    questions: workspaceData.questions,
  });
  const workspaceActions = useProjectWorkspaceActions({
    token,
    canWrite,
    selectedProjectId: workspaceData.selectedProjectId,
    refreshProjects: workspaceData.refreshProjects,
    refreshProjectData: workspaceData.refreshProjectData,
    setBusy,
    setFlash,
    setSelectedProjectId: workspaceData.setSelectedProjectId,
    setSessions: workspaceData.setSessions,
    projectName: workspaceForms.projectName,
    setProjectName: workspaceForms.setProjectName,
    projectDescription: workspaceForms.projectDescription,
    setProjectDescription: workspaceForms.setProjectDescription,
    questionText: workspaceForms.questionText,
    setQuestionText: workspaceForms.setQuestionText,
    questionType: workspaceForms.questionType,
    questionHypothesis: workspaceForms.questionHypothesis,
    setQuestionHypothesis: workspaceForms.setQuestionHypothesis,
    noteText: workspaceForms.noteText,
    setNoteText: workspaceForms.setNoteText,
    uploadFile: workspaceForms.uploadFile,
    setUploadFile: workspaceForms.setUploadFile,
    uploadTranscript: workspaceForms.uploadTranscript,
    setUploadTranscript: workspaceForms.setUploadTranscript,
    uploadTargetQuestionId: workspaceForms.uploadTargetQuestionId,
    setUploadTargetQuestionId: workspaceForms.setUploadTargetQuestionId,
    sessionType: workspaceForms.sessionType,
    sessionPrimaryQuestionId: workspaceForms.sessionPrimaryQuestionId,
  });

  return {
    ...workspaceActions,
    ...workspaceData,
    ...workspaceForms,
    datasetPrimaryQuestionOptions: workspaceData.questions,
  };
}

export { useProjectWorkspace };
