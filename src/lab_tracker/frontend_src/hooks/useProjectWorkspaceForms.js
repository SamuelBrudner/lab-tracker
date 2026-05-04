import * as React from "react";

const { useEffect, useState } = React;

function useProjectWorkspaceForms({ questions }) {
  const [projectName, setProjectName] = useState("");
  const [projectDescription, setProjectDescription] = useState("");

  const [questionText, setQuestionText] = useState("");
  const [questionType, setQuestionType] = useState("descriptive");
  const [questionHypothesis, setQuestionHypothesis] = useState("");
  const [questionParentIds, setQuestionParentIds] = useState([]);

  const [noteText, setNoteText] = useState("");
  const [uploadFile, setUploadFile] = useState(null);
  const [uploadTranscript, setUploadTranscript] = useState("");
  const [uploadTargetQuestionId, setUploadTargetQuestionId] = useState("");

  const [sessionType, setSessionType] = useState("scientific");
  const [sessionPrimaryQuestionId, setSessionPrimaryQuestionId] = useState("");

  useEffect(() => {
    if (sessionType !== "scientific") {
      return;
    }
    const nextActiveQuestions = questions.filter((item) => item.status === "active");
    if (nextActiveQuestions.length === 0) {
      if (!questions.some((item) => item.question_id === sessionPrimaryQuestionId)) {
        setSessionPrimaryQuestionId("");
      }
      return;
    }
    const hasCurrent = nextActiveQuestions.some(
      (item) => item.question_id === sessionPrimaryQuestionId
    );
    if (!hasCurrent) {
      setSessionPrimaryQuestionId(nextActiveQuestions[0].question_id);
    }
  }, [questions, sessionPrimaryQuestionId, sessionType]);

  useEffect(() => {
    const validQuestionIds = new Set(questions.map((item) => item.question_id));
    setQuestionParentIds((current) => {
      const next = current.filter((questionId) => validQuestionIds.has(questionId));
      return next.length === current.length ? current : next;
    });
  }, [questions]);

  return {
    noteText,
    projectDescription,
    projectName,
    questionHypothesis,
    questionParentIds,
    questionText,
    questionType,
    sessionPrimaryQuestionId,
    sessionType,
    setNoteText,
    setProjectDescription,
    setProjectName,
    setQuestionHypothesis,
    setQuestionParentIds,
    setQuestionText,
    setQuestionType,
    setSessionPrimaryQuestionId,
    setSessionType,
    setUploadFile,
    setUploadTargetQuestionId,
    setUploadTranscript,
    uploadFile,
    uploadTargetQuestionId,
    uploadTranscript,
  };
}

export { useProjectWorkspaceForms };
