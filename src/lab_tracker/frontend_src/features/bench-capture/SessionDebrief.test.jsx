import * as React from "react";

import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { createMemoryStorage, createUploadQueue } from "../../shared/upload-queue.js";
import { apiResponse, errorResponse, installFetchMock } from "../../test/utils.js";
import { DEBRIEF_PROMPTS, SessionDebrief } from "./SessionDebrief.jsx";

const SESSION = {
  project_id: "project-1",
  session_id: "session-1",
  session_type: "scientific",
  started_at: "2026-09-28T08:00:00Z",
  status: "closed",
};

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
    this.listeners.dataavailable?.({ data: new Blob(["debrief audio"], { type: this.mimeType }) });
    this.listeners.stop?.();
  }
}

function installMicrophone() {
  const track = { stop: vi.fn() };
  Object.defineProperty(navigator, "mediaDevices", {
    configurable: true,
    value: { getUserMedia: vi.fn().mockResolvedValue({ getTracks: () => [track] }) },
  });
  vi.stubGlobal("MediaRecorder", FakeMediaRecorder);
  return track;
}

function installUploadRoute({ uploads, offline = { value: false }, fail = { value: false } }) {
  return installFetchMock([
    {
      method: "POST",
      match: "/notes/upload-file",
      response: (request) => {
        if (offline.value) {
          throw new TypeError("Failed to fetch");
        }
        if (fail.value) {
          return errorResponse("Upload rejected.", 500);
        }
        const body = request.init.body;
        uploads.push({
          clientCaptureId: body.get("client_capture_id"),
          file: body.get("file"),
          metadata: JSON.parse(body.get("metadata")),
          projectId: body.get("project_id"),
          targets: JSON.parse(body.get("targets")),
        });
        return apiResponse({ note_id: "debrief-note" }, 201);
      },
    },
  ]);
}

function renderDebrief(overrides = {}) {
  const props = {
    token: "token-1",
    ownerId: "owner-1",
    projectId: "project-1",
    session: SESSION,
    canWrite: true,
    onDone: vi.fn(),
    queue: createUploadQueue({ storage: createMemoryStorage(), fetch: vi.fn() }),
    now: () => Date.parse("2026-09-28T17:00:00Z"),
    ...overrides,
  };
  return { props, ...render(<SessionDebrief {...props} />) };
}

afterEach(() => {
  delete navigator.mediaDevices;
});

describe("SessionDebrief", () => {
  it("asks the three prompts and records one staged debrief memo for the session", async () => {
    installMicrophone();
    const uploads = [];
    installUploadRoute({ uploads });
    renderDebrief();

    for (const prompt of DEBRIEF_PROMPTS) {
      expect(screen.getByText(prompt)).toBeInTheDocument();
    }
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Record debrief" }));
    });
    fireEvent.click(await screen.findByRole("button", { name: "Stop and save" }));

    expect(await screen.findByText("Debrief saved for review.")).toBeInTheDocument();
    expect(uploads).toHaveLength(1);
    expect(uploads[0].projectId).toBe("project-1");
    expect(uploads[0].targets).toEqual([{ entity_id: "session-1", entity_type: "session" }]);
    expect(uploads[0].file.name).toBe("session-debrief.webm");
    expect(uploads[0].metadata).toMatchObject({
      capture_channel: "debrief",
      capture_kind: "voice",
      capture_purpose: "session_debrief",
      capture_source: "mobile_capture",
      captured_at: "2026-09-28T17:00:00.000Z",
      transcript_status: "pending",
      voice_note_type: "Session debrief",
    });
    expect(uploads[0].metadata.capture_hint).toMatch(/what surprised you/);
    expect(screen.getByRole("button", { name: "Done" })).toBeInTheDocument();
  });

  it("is skippable in one tap, even mid-recording, without uploading anything", async () => {
    installMicrophone();
    const uploads = [];
    installUploadRoute({ uploads });
    const { props } = renderDebrief();

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Record debrief" }));
    });
    await screen.findByRole("button", { name: "Stop and save" });
    fireEvent.click(screen.getByRole("button", { name: "Skip" }));

    expect(props.onDone).toHaveBeenCalledWith("skipped");
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 20));
    });
    expect(uploads).toEqual([]);
  });

  it("falls back to the phone's recorder app and queues the memo offline", async () => {
    const offline = { value: true };
    const queue = createUploadQueue({ storage: createMemoryStorage(), fetch: vi.fn() });
    installUploadRoute({ uploads: [], offline });
    renderDebrief({ queue });

    const input = screen.getByLabelText("Record debrief");
    expect(input).toHaveAttribute("accept", "audio/*");
    fireEvent.change(input, {
      target: { files: [new File(["m4a"], "Recording.m4a", { type: "audio/mp4" })] },
    });

    expect(await screen.findByText(/Debrief queued/)).toBeInTheDocument();
    const [job] = await queue.listPending();
    expect(JSON.parse(job.fields.metadata)).toMatchObject({
      capture_channel: "debrief",
      capture_purpose: "session_debrief",
    });
    expect(JSON.parse(job.fields.targets)).toEqual([
      { entity_id: "session-1", entity_type: "session" },
    ]);
  });

  it("retries a failed upload under the same capture id", async () => {
    const fail = { value: true };
    const uploads = [];
    const fetchMock = installUploadRoute({ uploads, fail });
    renderDebrief();

    fireEvent.change(screen.getByLabelText("Record debrief"), {
      target: { files: [new File(["m4a"], "Recording.m4a", { type: "audio/mp4" })] },
    });
    expect(await screen.findByText("Upload rejected.")).toBeInTheDocument();

    fail.value = false;
    fireEvent.click(screen.getByRole("button", { name: "Retry upload" }));

    expect(await screen.findByText("Debrief saved for review.")).toBeInTheDocument();
    const ids = fetchMock.mock.calls.map(([, init]) => init.body.get("client_capture_id"));
    expect(ids).toHaveLength(2);
    expect(ids[0]).toBe(ids[1]);
  });

  it("cannot record without write access but can still be dismissed", () => {
    installUploadRoute({ uploads: [] });
    const { props } = renderDebrief({ canWrite: false });

    expect(screen.getByLabelText("Record debrief")).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Skip" }));
    expect(props.onDone).toHaveBeenCalledWith("skipped");
  });

  it("reports a refused microphone", async () => {
    Object.defineProperty(navigator, "mediaDevices", {
      configurable: true,
      value: { getUserMedia: vi.fn().mockRejectedValue(new Error("NotAllowedError")) },
    });
    vi.stubGlobal("MediaRecorder", FakeMediaRecorder);
    installUploadRoute({ uploads: [] });
    renderDebrief();

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Record debrief" }));
    });

    await waitFor(() =>
      expect(screen.getByText(/Could not access the microphone/)).toBeInTheDocument()
    );
  });
});
