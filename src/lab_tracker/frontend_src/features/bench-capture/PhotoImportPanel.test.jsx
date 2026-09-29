import * as React from "react";

import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { createMemoryStorage, createUploadQueue } from "../../shared/upload-queue.js";
import { apiResponse, errorResponse, installFetchMock } from "../../test/utils.js";
import { PhotoImportPanel, summaryText } from "./PhotoImportPanel.jsx";

const SESSION = {
  project_id: "project-1",
  session_id: "session-1",
  session_type: "operational",
  started_at: "2026-09-28T08:00:00Z",
  status: "closed",
};
const TAKEN_AT = Date.parse("2026-09-28T09:30:00Z");

function photo(name) {
  return new File([`bytes of ${name}`], name, { type: "image/jpeg", lastModified: TAKEN_AT });
}

function memoryQueue() {
  return createUploadQueue({
    storage: createMemoryStorage(),
    fetch: (...args) => globalThis.fetch(...args),
  });
}

function installRoutes({ uploads, texts, failing = new Set(), offline = { value: false } }) {
  return installFetchMock([
    {
      method: "POST",
      match: "/notes/upload-file",
      response: (request) => {
        if (offline.value) {
          throw new TypeError("Failed to fetch");
        }
        const body = request.init.body;
        const name = body.get("file").name;
        if (failing.has(name)) {
          return errorResponse("Storage is full.", 507);
        }
        uploads.push({
          clientCaptureId: body.get("client_capture_id"),
          metadata: JSON.parse(body.get("metadata")),
          name,
          targets: JSON.parse(body.get("targets")),
        });
        return apiResponse({ note_id: `photo-note-${uploads.length}` }, 201);
      },
    },
    {
      method: "POST",
      match: "/notes",
      response: (request) => {
        if (offline.value) {
          throw new TypeError("Failed to fetch");
        }
        texts.push(JSON.parse(request.init.body));
        return apiResponse({ note_id: `summary-${texts.length}` }, 201);
      },
    },
  ]);
}

function renderPanel(overrides = {}) {
  const props = {
    token: "token-1",
    ownerId: "owner-1",
    projectId: "project-1",
    session: SESSION,
    canWrite: true,
    queue: memoryQueue(),
    now: () => Date.parse("2026-09-28T18:00:00Z"),
    ...overrides,
  };
  return { props, ...render(<PhotoImportPanel {...props} />) };
}

function pickPhotos(files) {
  fireEvent.change(screen.getByLabelText("Photos to import"), { target: { files } });
}

function photoRow(name) {
  return within(screen.getByRole("list", { name: "Imported photos" }))
    .getByText(name)
    .closest("li");
}

describe("PhotoImportPanel", () => {
  it("offers a multi-select image picker", () => {
    installRoutes({ uploads: [], texts: [] });
    renderPanel();

    const input = screen.getByLabelText("Photos to import");
    expect(input).toHaveAttribute("multiple");
    expect(input).toHaveAttribute("accept", "image/*");
    expect(screen.getByRole("button", { name: "Import photos" })).toBeEnabled();
  });

  it("uploads every photo as one grouped, session-linked import plus a summary note", async () => {
    const uploads = [];
    const texts = [];
    installRoutes({ uploads, texts });
    renderPanel();

    // A non-image that slipped past the picker's filter is left out.
    pickPhotos([
      photo("gel-1.jpg"),
      new File(["plain"], "notes.txt", { type: "text/plain" }),
      photo("gel-2.jpg"),
    ]);

    await waitFor(() => expect(texts).toHaveLength(1));
    expect(uploads.map((upload) => upload.name)).toEqual(["gel-1.jpg", "gel-2.jpg"]);
    const bundleIds = new Set(uploads.map((upload) => upload.metadata.capture_bundle_id));
    expect(bundleIds.size).toBe(1);
    const [bundleId] = bundleIds;
    for (const [index, upload] of uploads.entries()) {
      expect(upload.targets).toEqual([{ entity_id: "session-1", entity_type: "session" }]);
      expect(upload.metadata).toMatchObject({
        capture_channel: "import",
        capture_import_index: index + 1,
        capture_import_total: 2,
        capture_kind: "image",
        capture_source: "mobile_capture",
        // The photo's own clock, not the import time.
        captured_at: "2026-09-28T09:30:00.000Z",
      });
    }
    expect(new Set(uploads.map((upload) => upload.clientCaptureId)).size).toBe(2);
    expect(texts[0]).toMatchObject({
      project_id: "project-1",
      targets: [{ entity_id: "session-1", entity_type: "session" }],
      metadata: {
        capture_bundle_id: bundleId,
        capture_channel: "import",
        capture_import_summary: true,
        capture_import_total: 2,
      },
    });
    expect(texts[0].raw_content).toMatch(/^Imported 2 photos from Operational session .*: gel-1\.jpg, gel-2\.jpg\.$/);
    expect(await screen.findByText(/2 of 2 done · 2 uploaded · summary note saved/)).toBeInTheDocument();
    expect(screen.getByLabelText("Photo import progress")).toHaveAttribute("value", "2");
  });

  it("marks a failed photo and retries just that one under the same capture id", async () => {
    const uploads = [];
    const texts = [];
    const failing = new Set(["gel-2.jpg"]);
    const fetchMock = installRoutes({ uploads, texts, failing });
    renderPanel();

    pickPhotos([photo("gel-1.jpg"), photo("gel-2.jpg"), photo("gel-3.jpg")]);

    await waitFor(() => expect(texts).toHaveLength(1));
    expect(within(photoRow("gel-2.jpg")).getByText("Failed")).toBeInTheDocument();
    expect(within(photoRow("gel-2.jpg")).getByText(/Storage is full/)).toBeInTheDocument();
    expect(screen.getByText(/3 of 3 done · 2 uploaded · 1 failed/)).toBeInTheDocument();

    failing.clear();
    fireEvent.click(screen.getByRole("button", { name: "Retry gel-2.jpg" }));

    await waitFor(() =>
      expect(within(photoRow("gel-2.jpg")).getByText("Uploaded")).toBeInTheDocument()
    );
    const attempts = fetchMock.mock.calls
      .filter(([url]) => url === "/notes/upload-file")
      .map(([, init]) => init.body)
      .filter((body) => body.get("file").name === "gel-2.jpg")
      .map((body) => body.get("client_capture_id"));
    expect(attempts).toHaveLength(2);
    expect(attempts[0]).toBe(attempts[1]);
    // The summary note was written once, not per retry.
    expect(texts).toHaveLength(1);
  });

  it("queues every photo and the summary offline", async () => {
    const offline = { value: true };
    const queue = memoryQueue();
    installRoutes({ uploads: [], texts: [], offline });
    renderPanel({ queue });

    pickPhotos([photo("gel-1.jpg"), photo("gel-2.jpg")]);

    await waitFor(() => expect(screen.getByText(/2 queued offline/)).toBeInTheDocument());
    const pending = await queue.listPending();
    expect(pending).toHaveLength(3);
    expect(pending.map((job) => job.endpoint)).toEqual([
      "/notes/upload-file",
      "/notes/upload-file",
      "/notes",
    ]);
    expect(JSON.parse(pending[0].fields.targets)).toEqual([
      { entity_id: "session-1", entity_type: "session" },
    ]);
    expect(pending[2].json.metadata.capture_import_summary).toBe(true);
  });

  it("is unavailable without write access", () => {
    installRoutes({ uploads: [], texts: [] });
    renderPanel({ canWrite: false });

    expect(screen.getByRole("button", { name: "Import photos" })).toBeDisabled();
  });

  it("names at most twenty files in the summary", () => {
    const items = Array.from({ length: 23 }, (_, index) => ({ name: `p${index + 1}.jpg` }));
    const text = summaryText({ items, sessionLabel: "", total: 23 });

    expect(text).toContain("p20.jpg, and 3 more.");
    expect(text).not.toContain("p21.jpg");
  });
});
