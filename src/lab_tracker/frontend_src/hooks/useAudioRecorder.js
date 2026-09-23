import * as React from "react";

import {
  audioExtensionForMime,
  canRecordAudio,
  pickAudioMimeType,
} from "../features/graph-drafts/format.js";

const { useCallback, useEffect, useRef, useState } = React;

/**
 * Browser microphone recording (getUserMedia + MediaRecorder) as one reusable
 * imperative lifecycle: start, stop, hand the finished audio to the caller as a
 * File, and never leak a live microphone stream.
 *
 * Shared by the review dictation console and the capture composer, so the
 * bench scientist can record a voice note in the page instead of being handed
 * off to the OS recorder app.
 *
 * @param {object} options
 * @param {boolean} [options.enabled] While false, an in-progress recording is
 *   stopped (the surface that owns the Stop control has gone away).
 * @param {string} [options.filenameBase] Stem for the produced File's name.
 * @param {(file: File, blob: Blob) => void} options.onRecorded Called once per
 *   finished recording that produced audio bytes.
 * @param {(message: string, error?: string) => void} options.setFlash
 * @param {() => void} [options.beforeStart] Runs after the microphone was
 *   granted and before recording begins (e.g. to stop narration).
 */
function useAudioRecorder({
  enabled = true,
  filenameBase = "recording",
  onRecorded,
  setFlash,
  beforeStart = null,
}) {
  const [isRecording, setIsRecording] = useState(false);
  const mediaRecorderRef = useRef(null);
  const audioChunksRef = useRef([]);
  const audioStreamRef = useRef(null);
  const startingRef = useRef(false);
  const mountedRef = useRef(true);
  // The stop handler is registered once per recording; read the latest
  // callback through a ref so a re-render mid-recording cannot strand it.
  const onRecordedRef = useRef(onRecorded);
  useEffect(() => {
    onRecordedRef.current = onRecorded;
  });

  const recordingSupported = canRecordAudio();

  const stopAudioStream = useCallback(() => {
    const stream = audioStreamRef.current;
    if (stream) {
      stream.getTracks().forEach((track) => track.stop());
      audioStreamRef.current = null;
    }
  }, []);

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
    if (typeof beforeStart === "function") {
      beforeStart();
    }
    setFlash("", "");
    let microphoneGranted = false;
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      microphoneGranted = true;
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
          const file = new File([blob], `${filenameBase}.${audioExtensionForMime(type)}`, {
            type,
          });
          onRecordedRef.current?.(file, blob);
        }
        setIsRecording(false);
      });
      // Publish the recorder only once it has started: a recorder whose
      // start() threw must not stay in the ref, or every later start is
      // refused as already recording until remount.
      recorder.start();
      mediaRecorderRef.current = recorder;
      setIsRecording(true);
    } catch (err) {
      mediaRecorderRef.current = null;
      stopAudioStream();
      if (mountedRef.current) {
        setIsRecording(false);
        // Only a refused getUserMedia is a permissions problem; a recorder that
        // failed after access was granted needs its own, truthful message.
        setFlash(
          "",
          microphoneGranted
            ? `Could not start recording: ${err?.message || "the recorder failed to start."}`
            : "Could not access the microphone. Check browser permissions."
        );
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

  // If the owning surface stops being usable mid-recording (its Stop control
  // is gated on `enabled`), auto-stop rather than strand a live microphone.
  useEffect(() => {
    if (isRecording && !enabled) {
      stopRecording();
    }
  }, [enabled, isRecording, stopRecording]);

  function toggleRecording() {
    if (isRecording) {
      stopRecording();
    } else {
      startRecording();
    }
  }

  return {
    isRecording,
    recordingSupported,
    startRecording,
    stopRecording,
    toggleRecording,
  };
}

export { useAudioRecorder };
