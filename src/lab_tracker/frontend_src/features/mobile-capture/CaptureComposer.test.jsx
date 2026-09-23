import * as React from "react";

import { render, screen } from "@testing-library/react";
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
