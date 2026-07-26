import { describe, expect, it, vi } from "vitest";

import { apiResponse, errorResponse, installFetchMock } from "../test/utils.js";
import { UPLOAD_FILE_PATH } from "./upload-queue.js";
import {
  OFFLINE_QUEUED,
  buildCaptureMetadata,
  buildTargets,
  createTextCapture,
  queueRawFileNoteOffline,
  sourceFileMetadata,
  uploadOrQueueRawFile,
  uploadRawFileNote,
} from "./capture-upload.js";

function fakeQueue() {
  return { enqueue: vi.fn(async () => {}) };
}

describe("buildTargets", () => {
  it("includes only the selected entity ids", () => {
    expect(buildTargets({ questionId: "q1", datasetId: "d1", claimId: "" })).toEqual([
      { entity_id: "q1", entity_type: "question" },
      { entity_id: "d1", entity_type: "dataset" },
    ]);
  });

  it("returns an empty array when nothing is selected", () => {
    expect(buildTargets({})).toEqual([]);
  });
});

describe("sourceFileMetadata", () => {
  it("returns empty for no file", () => {
    expect(sourceFileMetadata(null)).toEqual({});
  });

  it("derives last-modified metadata from the file stamp", () => {
    const meta = sourceFileMetadata({ lastModified: 1_700_000_000_000 });
    expect(meta.source_file_last_modified_ms).toBe(1_700_000_000_000);
    expect(meta.source_file_last_modified_at).toBe(new Date(1_700_000_000_000).toISOString());
  });
});

describe("buildCaptureMetadata", () => {
  it("stamps voice notes with type and pending transcript status", () => {
    const meta = buildCaptureMetadata({
      captureMode: "voice",
      kind: "voice",
      hint: "  rig 2  ",
      voiceNoteType: "Observation",
    });
    expect(meta).toMatchObject({
      capture_source: "mobile_capture",
      capture_mode: "voice",
      capture_kind: "voice",
      capture_review_status: "pending_review",
      capture_hint: "rig 2",
      voice_note_type: "Observation",
      transcript_status: "pending",
    });
  });

  it("adds a bundle id and omits voice fields for image kinds", () => {
    const meta = buildCaptureMetadata({ captureMode: "bundle", kind: "image", bundleId: "b1" });
    expect(meta.capture_bundle_id).toBe("b1");
    expect(meta.voice_note_type).toBeUndefined();
    expect(meta.transcript_status).toBeUndefined();
  });
});

describe("uploadRawFileNote", () => {
  it("posts multipart fields to the upload endpoint", async () => {
    const fetchMock = installFetchMock([
      { match: "/notes/upload-file", method: "POST", response: apiResponse({ note_id: "n1" }) },
    ]);
    const result = await uploadRawFileNote({
      token: "t",
      projectId: "p1",
      fileToUpload: new Blob(["x"]),
      metadata: { capture_kind: "image" },
      clientCaptureId: "c1",
      targets: [{ entity_id: "q1", entity_type: "question" }],
    });
    expect(result).toEqual({ note_id: "n1" });
    const body = fetchMock.mock.calls[0][1].body;
    expect(body.get("project_id")).toBe("p1");
    expect(body.get("client_capture_id")).toBe("c1");
    expect(JSON.parse(body.get("metadata"))).toEqual({ capture_kind: "image" });
    expect(JSON.parse(body.get("targets"))).toHaveLength(1);
  });
});

describe("uploadOrQueueRawFile", () => {
  it("returns the created note on a successful upload without queueing", async () => {
    installFetchMock([
      { match: "/notes/upload-file", method: "POST", response: apiResponse({ note_id: "n1" }) },
    ]);
    const queue = fakeQueue();
    const result = await uploadOrQueueRawFile({
      token: "t",
      projectId: "p1",
      ownerId: "owner-1",
      fileToUpload: new Blob(["x"]),
      metadata: {},
      queue,
    });
    expect(result).toEqual({ note_id: "n1" });
    expect(queue.enqueue).not.toHaveBeenCalled();
  });

  it("queues offline when the request fails with no HTTP status", async () => {
    // No matching route -> fetch itself throws (a network-style failure with no
    // .status), which is the only case that should fall back to the queue.
    installFetchMock([]);
    const queue = fakeQueue();
    const result = await uploadOrQueueRawFile({
      token: "t",
      projectId: "p1",
      ownerId: "owner-1",
      fileToUpload: new Blob(["x"]),
      metadata: { capture_kind: "voice" },
      targets: [{ entity_id: "q1", entity_type: "question" }],
      queue,
    });
    expect(result).toBe(OFFLINE_QUEUED);
    expect(queue.enqueue).toHaveBeenCalledTimes(1);
    const enqueued = queue.enqueue.mock.calls[0][0];
    expect(enqueued.endpoint).toBe(UPLOAD_FILE_PATH);
    expect(enqueued.ownerId).toBe("owner-1");
    expect(enqueued.fields.project_id).toBe("p1");
    expect(JSON.parse(enqueued.fields.targets)).toHaveLength(1);
  });

  it("rethrows a server rejection instead of queueing", async () => {
    installFetchMock([
      { match: "/notes/upload-file", method: "POST", response: errorResponse("bad request", 400) },
    ]);
    const queue = fakeQueue();
    await expect(
      uploadOrQueueRawFile({
        token: "t",
        projectId: "p1",
        ownerId: "owner-1",
        fileToUpload: new Blob(["x"]),
        metadata: {},
        queue,
      })
    ).rejects.toMatchObject({ status: 400 });
    expect(queue.enqueue).not.toHaveBeenCalled();
  });
});

describe("queueRawFileNoteOffline", () => {
  it("returns false when there is no queue", async () => {
    expect(
      await queueRawFileNoteOffline({
        projectId: "p1",
        fileToUpload: new Blob(["x"]),
        metadata: {},
        clientCaptureId: "c1",
        queue: null,
      })
    ).toBe(false);
  });
});

describe("createTextCapture", () => {
  it("posts a JSON note body", async () => {
    const fetchMock = installFetchMock([
      { match: "/notes", method: "POST", response: apiResponse({ note_id: "n1" }) },
    ]);
    const result = await createTextCapture({
      token: "t",
      projectId: "p1",
      rawContent: "a note",
      targets: [],
      metadata: { capture_kind: "text" },
    });
    expect(result).toEqual({ note_id: "n1" });
    const body = JSON.parse(fetchMock.mock.calls[0][1].body);
    expect(body).toEqual({
      project_id: "p1",
      raw_content: "a note",
      targets: [],
      metadata: { capture_kind: "text" },
    });
  });
});
