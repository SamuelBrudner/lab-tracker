import * as React from "react";

import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { CaptureComposer } from "./CaptureComposer.jsx";

function renderComposer(overrides = {}) {
  return render(
    <CaptureComposer
      attachmentMenuOpen={true}
      audioFile={null}
      canWrite={true}
      captureMode="text"
      composerTextValue=""
      navigate={vi.fn()}
      needsVoice={false}
      onAudioFileChange={vi.fn()}
      onClearAudioFile={vi.fn()}
      onClearPhotoFile={vi.fn()}
      onComposerTextChange={vi.fn()}
      onPhotoFileChange={vi.fn()}
      onStartBundleCapture={vi.fn()}
      onStartTextCapture={vi.fn()}
      onUploadCapture={vi.fn()}
      photoFile={null}
      readyToCapture={false}
      setAttachmentMenuOpen={vi.fn()}
      setVoiceNoteType={vi.fn()}
      voiceNoteType="Observation"
      {...overrides}
    />
  );
}

describe("CaptureComposer accessibility", () => {
  it("gives the visible microphone control an accessible name", () => {
    const { container } = renderComposer();

    const micControl = container.querySelector('label[for="capture-audio-record-input"]');
    expect(micControl).not.toBeNull();
    expect(micControl).toHaveTextContent("Record voice note");
  });

  it("exposes the attachment menu as a named group", () => {
    renderComposer();

    expect(screen.getByRole("group", { name: "Attachment options" })).toHaveAttribute(
      "id",
      "capture-attachment-menu"
    );
  });
});

describe("CaptureComposer in-page recording", () => {
  it("turns the microphone into a record/stop toggle when the browser can record", () => {
    const onToggleRecording = vi.fn();
    const { container, rerender } = renderComposer({
      recordingSupported: true,
      onToggleRecording,
    });

    expect(container.querySelector('label[for="capture-audio-record-input"]')).toBeNull();
    const mic = screen.getByRole("button", { name: "Record voice note" });
    expect(mic).toHaveAttribute("aria-pressed", "false");
    fireEvent.click(mic);
    expect(onToggleRecording).toHaveBeenCalledTimes(1);

    rerender(
      <CaptureComposer
        attachmentMenuOpen={false}
        audioFile={null}
        canWrite={true}
        captureMode="text"
        composerTextValue=""
        navigate={vi.fn()}
        needsVoice={false}
        onAudioFileChange={vi.fn()}
        onClearAudioFile={vi.fn()}
        onClearPhotoFile={vi.fn()}
        onComposerTextChange={vi.fn()}
        onPhotoFileChange={vi.fn()}
        onStartBundleCapture={vi.fn()}
        onStartTextCapture={vi.fn()}
        onUploadCapture={vi.fn()}
        photoFile={null}
        readyToCapture={false}
        setAttachmentMenuOpen={vi.fn()}
        setVoiceNoteType={vi.fn()}
        voiceNoteType="Observation"
        recordingSupported={true}
        isRecording={true}
        onToggleRecording={onToggleRecording}
      />
    );
    const stop = screen.getByRole("button", { name: "Stop recording" });
    expect(stop).toHaveAttribute("aria-pressed", "true");
    expect(stop).toHaveClass("recording");
    expect(screen.getByText(/Recording… tap the microphone to stop/)).toBeInTheDocument();
  });

  it("offers to restore an unsent capture and routes both choices to the controller", () => {
    const onRestoreDraft = vi.fn();
    const onDiscardDraft = vi.fn();
    renderComposer({ draftSavedAt: Date.now(), onRestoreDraft, onDiscardDraft });

    expect(screen.getByText(/an unsent capture/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Restore them" }));
    expect(onRestoreDraft).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole("button", { name: "Discard them" }));
    expect(onDiscardDraft).toHaveBeenCalledTimes(1);
  });
});
