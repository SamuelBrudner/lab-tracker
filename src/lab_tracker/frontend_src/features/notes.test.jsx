import * as React from "react";

import { fireEvent, render, screen, waitFor } from "@testing-library/react";

import { apiResponse, note } from "../test/fixtures.js";
import { installFetchMock } from "../test/utils.js";
import { NoteDetailCard } from "./notes.jsx";

const NOTE_ID = "note-voice";
const ADMIN = { role: "admin", user_id: "admin-1", username: "admin" };
const AUDIO_ASSET = {
  checksum: "abc",
  content_type: "audio/webm",
  filename: "voice.webm",
  size_bytes: 12,
  storage_id: "storage-voice",
};
const CAPTURE_METADATA = {
  capture_kind: "voice",
  capture_source: "mobile_capture",
};
const PROVENANCE = {
  transcript_generated_at: "2026-09-21T10:00:00Z",
  transcript_model: "whisper-1",
  transcript_provider: "openai",
  transcript_source_storage_id: "storage-voice",
};

function voiceNote({ metadata, transcribedText }) {
  return note({
    metadata,
    noteId: NOTE_ID,
    rawAsset: AUDIO_ASSET,
    transcribedText,
  });
}

function patchedNote(request) {
  const body = JSON.parse(request.init.body);
  return apiResponse(
    voiceNote({ metadata: body.metadata, transcribedText: body.transcribed_text.trim() })
  );
}

function baseRoutes({ noteResponses, onPatch, onRawFetch = () => {}, extra = [] }) {
  return [
    { match: `/notes/${NOTE_ID}`, response: noteResponses },
    {
      match: `/notes/${NOTE_ID}/raw`,
      response: () => {
        onRawFetch();
        return apiResponse({
          ...AUDIO_ASSET,
          content_base64: "dm9pY2U=",
        });
      },
    },
    { match: "/projects/project-1/members", response: apiResponse([]) },
    {
      match: `/notes/${NOTE_ID}/transcript`,
      method: "POST",
      response: apiResponse(
        voiceNote({
          metadata: { ...CAPTURE_METADATA, ...PROVENANCE, transcript_status: "ready" },
          transcribedText: "Provider transcript",
        })
      ),
    },
    {
      match: `/notes/${NOTE_ID}`,
      method: "PATCH",
      response: (request) => {
        onPatch(JSON.parse(request.init.body));
        return patchedNote(request);
      },
    },
    ...extra,
  ];
}

function renderDetail(props = {}) {
  const setFlash = vi.fn();
  const navigate = vi.fn();
  render(
    <NoteDetailCard
      token="token-1"
      noteId={NOTE_ID}
      projects={[{ name: "Project One", project_id: "project-1" }]}
      navigate={navigate}
      onSetActiveProject={vi.fn()}
      canWrite={true}
      user={ADMIN}
      setBusy={vi.fn()}
      setFlash={setFlash}
      {...props}
    />
  );
  return { navigate, setFlash };
}

async function waitForLoadedVoiceNote() {
  await screen.findByRole("button", { name: "Transcribe voice" });
}

describe("NoteDetailCard transcript provenance", () => {
  it("keeps provider provenance when a transcript is saved after transcription", async () => {
    const patches = [];
    const pendingNote = apiResponse(
      voiceNote({
        metadata: { ...CAPTURE_METADATA, transcript_status: "pending" },
        transcribedText: "",
      })
    );
    const transcribedNote = apiResponse(
      voiceNote({
        metadata: { ...CAPTURE_METADATA, ...PROVENANCE, transcript_status: "ready" },
        transcribedText: "Provider transcript",
      })
    );
    installFetchMock(
      baseRoutes({
        noteResponses: [pendingNote, transcribedNote],
        onPatch: (body) => patches.push(body),
      })
    );
    const { setFlash } = renderDetail();
    await waitForLoadedVoiceNote();

    fireEvent.click(screen.getByRole("button", { name: "Transcribe voice" }));
    await waitFor(() => expect(setFlash).toHaveBeenCalledWith("Voice transcript ready."));
    fireEvent.change(screen.getByDisplayValue("Provider transcript"), {
      target: { value: "Provider transcript, corrected" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save transcript" }));

    await waitFor(() => expect(patches).toHaveLength(1));
    expect(patches[0].transcribed_text).toBe("Provider transcript, corrected");
    expect(patches[0].metadata).toMatchObject({
      ...CAPTURE_METADATA,
      ...PROVENANCE,
      transcript_status: "ready",
    });
    expect(patches[0].metadata.transcript_edited_at).toEqual(expect.any(String));
  });

  it("drafts from a fresh transcription without replaying stale metadata", async () => {
    const patches = [];
    const draftRequests = [];
    const pendingNote = apiResponse(
      voiceNote({
        metadata: { ...CAPTURE_METADATA, transcript_status: "pending" },
        transcribedText: "",
      })
    );
    let rawFetches = 0;
    installFetchMock(
      baseRoutes({
        noteResponses: [pendingNote],
        onPatch: (body) => patches.push(body),
        onRawFetch: () => {
          rawFetches += 1;
        },
        extra: [
          {
            match: `/notes/${NOTE_ID}/graph-drafts`,
            method: "POST",
            response: (request) => {
              draftRequests.push(JSON.parse(request.init.body));
              return apiResponse({ change_set_id: "draft-1", status: "ready" }, 201);
            },
          },
        ],
      })
    );
    const { navigate, setFlash } = renderDetail();
    await waitForLoadedVoiceNote();
    await waitFor(() => expect(rawFetches).toBe(1));

    fireEvent.click(screen.getByRole("button", { name: "Transcribe voice" }));
    await waitFor(() => expect(setFlash).toHaveBeenCalledWith("Voice transcript ready."));
    fireEvent.click(screen.getByRole("button", { name: "Draft graph update" }));

    await waitFor(() => expect(navigate).toHaveBeenCalledWith("/app/graph-drafts/draft-1"));
    expect(draftRequests).toEqual([{ mode: "graph_context" }]);
    // The transcript already matches the server copy, so nothing is re-PATCHed.
    expect(patches).toHaveLength(0);
    // Refreshing the note does not re-download the audio preview.
    expect(rawFetches).toBe(1);
  });

  it("builds the saved metadata from the server copy, not the page-load snapshot", async () => {
    const patches = [];
    // The page loaded before a background auto-transcription wrote provenance.
    const loadedNote = apiResponse(
      voiceNote({
        metadata: { ...CAPTURE_METADATA, transcript_status: "pending" },
        transcribedText: "",
      })
    );
    const serverNote = apiResponse(
      voiceNote({
        metadata: { ...CAPTURE_METADATA, ...PROVENANCE, transcript_status: "ready" },
        transcribedText: "Provider transcript",
      })
    );
    installFetchMock(
      baseRoutes({
        noteResponses: [loadedNote, serverNote],
        onPatch: (body) => patches.push(body),
      })
    );
    renderDetail();
    await waitForLoadedVoiceNote();

    fireEvent.change(screen.getByRole("textbox"), {
      target: { value: "Typed by the reviewer" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save transcript" }));

    await waitFor(() => expect(patches).toHaveLength(1));
    expect(patches[0].metadata).toMatchObject({ ...CAPTURE_METADATA, ...PROVENANCE });
    expect(patches[0].transcribed_text).toBe("Typed by the reviewer");
  });
});
