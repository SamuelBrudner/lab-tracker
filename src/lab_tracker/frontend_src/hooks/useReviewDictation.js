import * as React from "react";

import { canSpeakReview } from "../features/graph-drafts/format.js";
import { useAudioRecorder } from "./useAudioRecorder.js";

const { useCallback, useEffect, useRef, useState } = React;

// The "Listen & respond" console's browser-media surface: spoken review
// playback (speechSynthesis) and dictated feedback (recorded through the
// shared useAudioRecorder), plus the typed feedback / image-attachment inputs.
// Owning the imperative media lifecycle here keeps it out of the data
// controller and out of the view. canEditDraft is supplied by the workflow so
// a mid-recording status change can auto-release the microphone.
function useReviewDictation({ changeSetId, spokenReview, canEditDraft, setFlash }) {
  const [speechStatus, setSpeechStatus] = useState("idle");
  const [reviseAudio, setReviseAudio] = useState(null);
  const [reviseFeedback, setReviseFeedback] = useState("");
  const [reviseAttachments, setReviseAttachments] = useState([]);

  const speechUtteranceRef = useRef(null);
  const previousChangeSetIdRef = useRef(changeSetId);

  const speechSupported = canSpeakReview();

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

  const clearReviseAudio = useCallback(() => {
    setReviseAudio((current) => {
      if (current?.url) {
        URL.revokeObjectURL(current.url);
      }
      return null;
    });
  }, []);

  const resetReviseInputs = useCallback(() => {
    setReviseFeedback("");
    setReviseAttachments([]);
    clearReviseAudio();
  }, [clearReviseAudio]);

  // Drop the dictation inputs on a genuine route switch so a previous draft's
  // in-progress feedback never carries into the next one. Same-id reloads keep
  // the current edits, matching the data controller's reset behavior.
  useEffect(() => {
    if (previousChangeSetIdRef.current !== changeSetId) {
      previousChangeSetIdRef.current = changeSetId;
      resetReviseInputs();
    }
  }, [changeSetId, resetReviseInputs]);

  useEffect(
    () => () => {
      if (reviseAudio?.url) {
        URL.revokeObjectURL(reviseAudio.url);
      }
    },
    [reviseAudio]
  );

  const recorder = useAudioRecorder({
    enabled: canEditDraft,
    filenameBase: "dictated-feedback",
    // Narration and dictation share the audio channel; stop the former first.
    beforeStart: () => stopSpeech(),
    onRecorded: (file, blob) => {
      setReviseAudio((current) => {
        if (current?.url) {
          URL.revokeObjectURL(current.url);
        }
        return { file, url: URL.createObjectURL(blob) };
      });
    },
    setFlash,
  });

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

  return {
    speechStatus,
    speechSupported,
    recordingSupported: recorder.recordingSupported,
    isRecording: recorder.isRecording,
    reviseAudio,
    reviseFeedback,
    setReviseFeedback,
    reviseAttachments,
    toggleSpeech,
    stopSpeech,
    toggleRecording: recorder.toggleRecording,
    clearReviseAudio,
    handleAttachmentChange,
    removeAttachment,
    resetReviseInputs,
  };
}

export { useReviewDictation };
