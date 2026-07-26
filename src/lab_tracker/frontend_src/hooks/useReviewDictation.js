import * as React from "react";

import {
  audioExtensionForMime,
  canRecordAudio,
  canSpeakReview,
  pickAudioMimeType,
} from "../features/graph-drafts/format.js";

const { useCallback, useEffect, useRef, useState } = React;

// The "Listen & respond" console's browser-media surface: spoken review
// playback (speechSynthesis) and dictated feedback (getUserMedia +
// MediaRecorder), plus the typed feedback / image-attachment inputs. Owning the
// imperative media lifecycle here keeps it out of the data controller and out
// of the view. canEditDraft is supplied by the workflow so a mid-recording
// status change can auto-release the microphone.
function useReviewDictation({ changeSetId, spokenReview, canEditDraft, setFlash }) {
  const [speechStatus, setSpeechStatus] = useState("idle");
  const [isRecording, setIsRecording] = useState(false);
  const [reviseAudio, setReviseAudio] = useState(null);
  const [reviseFeedback, setReviseFeedback] = useState("");
  const [reviseAttachments, setReviseAttachments] = useState([]);

  const mediaRecorderRef = useRef(null);
  const audioChunksRef = useRef([]);
  const audioStreamRef = useRef(null);
  const startingRef = useRef(false);
  const speechUtteranceRef = useRef(null);
  const mountedRef = useRef(true);
  const previousChangeSetIdRef = useRef(changeSetId);

  const speechSupported = canSpeakReview();
  const recordingSupported = canRecordAudio();

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

  return {
    speechStatus,
    speechSupported,
    recordingSupported,
    isRecording,
    reviseAudio,
    reviseFeedback,
    setReviseFeedback,
    reviseAttachments,
    toggleSpeech,
    stopSpeech,
    toggleRecording,
    clearReviseAudio,
    handleAttachmentChange,
    removeAttachment,
    resetReviseInputs,
  };
}

export { useReviewDictation };
