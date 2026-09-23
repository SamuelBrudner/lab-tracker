import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { useAudioRecorder } from "./useAudioRecorder.js";

class FakeMediaRecorder {
  static isTypeSupported() {
    return true;
  }

  constructor(_stream, options = {}) {
    this.listeners = {};
    this.mimeType = options.mimeType || "audio/webm";
    this.state = "inactive";
  }

  addEventListener(name, callback) {
    this.listeners[name] = callback;
  }

  start() {
    this.state = "recording";
  }

  stop() {
    this.state = "inactive";
    this.listeners.dataavailable?.({
      data: new Blob(["voice bytes"], { type: this.mimeType }),
    });
    this.listeners.stop?.();
  }
}

function installMicrophone({ granted = true } = {}) {
  const track = { stop: vi.fn() };
  const getUserMedia = granted
    ? vi.fn().mockResolvedValue({ getTracks: () => [track] })
    : vi.fn().mockRejectedValue(new Error("NotAllowedError"));
  Object.defineProperty(navigator, "mediaDevices", {
    configurable: true,
    value: { getUserMedia },
  });
  vi.stubGlobal("MediaRecorder", FakeMediaRecorder);
  return { getUserMedia, track };
}

afterEach(() => {
  delete navigator.mediaDevices;
});

describe("useAudioRecorder", () => {
  it("records, hands the finished audio over as a named File, and releases the microphone", async () => {
    const { track } = installMicrophone();
    const onRecorded = vi.fn();
    const setFlash = vi.fn();
    const { result } = renderHook(() =>
      useAudioRecorder({ filenameBase: "voice-note", onRecorded, setFlash })
    );
    expect(result.current.recordingSupported).toBe(true);
    expect(result.current.isRecording).toBe(false);

    await act(async () => {
      await result.current.startRecording();
    });
    expect(result.current.isRecording).toBe(true);
    expect(setFlash).toHaveBeenCalledWith("", "");

    act(() => {
      result.current.stopRecording();
    });
    expect(result.current.isRecording).toBe(false);
    expect(onRecorded).toHaveBeenCalledTimes(1);
    const [file] = onRecorded.mock.calls[0];
    expect(file).toBeInstanceOf(File);
    expect(file.name).toBe("voice-note.webm");
    expect(file.type).toBe("audio/webm");
    expect(track.stop).toHaveBeenCalledTimes(1);
  });

  it("toggles between start and stop and ignores a second start while one is pending", async () => {
    const { getUserMedia } = installMicrophone();
    const { result } = renderHook(() =>
      useAudioRecorder({ onRecorded: vi.fn(), setFlash: vi.fn() })
    );

    await act(async () => {
      const first = result.current.toggleRecording();
      const second = result.current.toggleRecording();
      await Promise.all([first, second]);
    });
    expect(getUserMedia).toHaveBeenCalledTimes(1);
    expect(result.current.isRecording).toBe(true);

    act(() => {
      result.current.toggleRecording();
    });
    expect(result.current.isRecording).toBe(false);
  });

  it("stops a live recording when the owning surface is no longer enabled", async () => {
    const { track } = installMicrophone();
    const onRecorded = vi.fn();
    const { rerender, result } = renderHook(
      ({ enabled }) => useAudioRecorder({ enabled, onRecorded, setFlash: vi.fn() }),
      { initialProps: { enabled: true } }
    );
    await act(async () => {
      await result.current.startRecording();
    });
    expect(result.current.isRecording).toBe(true);

    rerender({ enabled: false });
    expect(result.current.isRecording).toBe(false);
    expect(track.stop).toHaveBeenCalledTimes(1);
    expect(onRecorded).toHaveBeenCalledTimes(1);
  });

  it("reports a refused microphone as a permissions problem and stays startable", async () => {
    installMicrophone({ granted: false });
    const setFlash = vi.fn();
    const { result } = renderHook(() => useAudioRecorder({ onRecorded: vi.fn(), setFlash }));

    await act(async () => {
      await result.current.startRecording();
    });
    expect(result.current.isRecording).toBe(false);
    expect(setFlash).toHaveBeenLastCalledWith(
      "",
      "Could not access the microphone. Check browser permissions."
    );

    installMicrophone();
    await act(async () => {
      await result.current.startRecording();
    });
    expect(result.current.isRecording).toBe(true);
  });

  it("says so when the browser cannot record at all", async () => {
    const setFlash = vi.fn();
    const { result } = renderHook(() => useAudioRecorder({ onRecorded: vi.fn(), setFlash }));
    expect(result.current.recordingSupported).toBe(false);

    await act(async () => {
      await result.current.startRecording();
    });
    expect(setFlash).toHaveBeenLastCalledWith(
      "",
      "This browser does not support microphone recording."
    );
  });
});
