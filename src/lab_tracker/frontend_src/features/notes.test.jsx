import * as React from "react";

import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

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

function baseRoutes({
  noteResponses,
  onPatch,
  onRawFetch = () => {},
  extra = [],
  transcriptResponse = null,
}) {
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
      response:
        transcriptResponse ||
        apiResponse(
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

  it("keeps an edit typed while the transcription response is being applied", async () => {
    // Deterministic form of a race: the user types in the same tick the
    // transcription response lands, before React flushes the effect that
    // syncs the editor from the refreshed note. The edit must survive.
    let releaseTranscript = null;
    installFetchMock(
      baseRoutes({
        noteResponses: [
          apiResponse(
            voiceNote({
              metadata: { ...CAPTURE_METADATA, transcript_status: "pending" },
              transcribedText: "",
            })
          ),
        ],
        onPatch: () => {},
        transcriptResponse: () =>
          new Promise((resolve) => {
            releaseTranscript = resolve;
          }),
      })
    );
    renderDetail();
    await waitForLoadedVoiceNote();
    fireEvent.click(screen.getByRole("button", { name: "Transcribe voice" }));
    await waitFor(() => expect(releaseTranscript).toBeTypeOf("function"));

    await act(async () => {
      releaseTranscript(
        apiResponse(
          voiceNote({
            metadata: { ...CAPTURE_METADATA, ...PROVENANCE, transcript_status: "ready" },
            transcribedText: "Provider transcript",
          })
        )
      );
      await new Promise((resolve) => setTimeout(resolve, 0));
      fireEvent.change(screen.getByRole("textbox"), {
        target: { value: "Typed right away" },
      });
    });

    expect(screen.getByRole("textbox")).toHaveValue("Typed right away");
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
    expect(draftRequests).toEqual([
      { external_provider_acknowledged: false, mode: "graph_context" },
    ]);
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
  it("does not apply a transcription for a note the user has moved away from", async () => {
    const OTHER_ID = "note-other";
    let releaseTranscript = null;
    installFetchMock([
      {
        match: `/notes/${NOTE_ID}`,
        response: apiResponse(
          voiceNote({
            metadata: { ...CAPTURE_METADATA, transcript_status: "pending" },
            transcribedText: "",
          })
        ),
      },
      {
        match: `/notes/${OTHER_ID}`,
        response: apiResponse(
          note({
            metadata: { ...CAPTURE_METADATA, transcript_status: "ready" },
            noteId: OTHER_ID,
            rawAsset: { ...AUDIO_ASSET, storage_id: "storage-other" },
            transcribedText: "Other note transcript",
          })
        ),
      },
      {
        match: /^\/notes\/note-(voice|other)\/raw$/,
        response: apiResponse({ ...AUDIO_ASSET, content_base64: "dm9pY2U=" }),
      },
      { match: "/projects/project-1/members", response: apiResponse([]) },
      {
        match: `/notes/${NOTE_ID}/transcript`,
        method: "POST",
        response: () =>
          new Promise((resolve) => {
            releaseTranscript = resolve;
          }),
      },
    ]);
    const setFlash = vi.fn();
    const props = {
      token: "token-1",
      projects: [{ name: "Project One", project_id: "project-1" }],
      navigate: vi.fn(),
      onSetActiveProject: vi.fn(),
      canWrite: true,
      user: ADMIN,
      setBusy: vi.fn(),
      setFlash,
    };
    const { rerender } = render(<NoteDetailCard {...props} noteId={NOTE_ID} />);
    await waitForLoadedVoiceNote();

    fireEvent.click(screen.getByRole("button", { name: "Transcribe voice" }));
    await waitFor(() => expect(releaseTranscript).toBeTypeOf("function"));
    rerender(<NoteDetailCard {...props} noteId={OTHER_ID} />);
    expect(await screen.findByDisplayValue("Other note transcript")).toBeInTheDocument();

    releaseTranscript(
      apiResponse(
        voiceNote({
          metadata: { ...CAPTURE_METADATA, ...PROVENANCE, transcript_status: "ready" },
          transcribedText: "Transcript of the first note",
        })
      )
    );
    await waitFor(() => expect(setFlash).toHaveBeenCalledWith("Voice transcript ready."));
    await new Promise((resolve) => setTimeout(resolve, 0));

    expect(screen.getByDisplayValue("Other note transcript")).toBeInTheDocument();
    expect(screen.queryByDisplayValue("Transcript of the first note")).not.toBeInTheDocument();
  });
  it("locks the transcript editor while a save is in flight", async () => {
    let releasePatch = null;
    installFetchMock([
      {
        match: `/notes/${NOTE_ID}`,
        response: [
          apiResponse(
            voiceNote({
              metadata: { ...CAPTURE_METADATA, transcript_status: "pending" },
              transcribedText: "",
            })
          ),
          apiResponse(
            voiceNote({
              metadata: { ...CAPTURE_METADATA, transcript_status: "pending" },
              transcribedText: "",
            })
          ),
        ],
      },
      {
        match: `/notes/${NOTE_ID}/raw`,
        response: apiResponse({ ...AUDIO_ASSET, content_base64: "dm9pY2U=" }),
      },
      { match: "/projects/project-1/members", response: apiResponse([]) },
      {
        match: `/notes/${NOTE_ID}`,
        method: "PATCH",
        response: (request) =>
          new Promise((resolve) => {
            releasePatch = () => resolve(patchedNote(request));
          }),
      },
    ]);
    const { setFlash } = renderDetail();
    await waitForLoadedVoiceNote();

    fireEvent.change(screen.getByRole("textbox"), { target: { value: "Saved text" } });
    fireEvent.click(screen.getByRole("button", { name: "Save transcript" }));
    await waitFor(() => expect(releasePatch).toBeTypeOf("function"));

    // Typing during the save would be overwritten by the saved server copy.
    expect(screen.getByRole("textbox")).toBeDisabled();

    releasePatch();
    await waitFor(() => expect(setFlash).toHaveBeenCalledWith("Transcript saved."));
    await waitFor(() => expect(screen.getByRole("textbox")).toBeEnabled());
    expect(screen.getByRole("textbox")).toHaveValue("Saved text");
  });
});

describe("NoteDetailCard external-provider consent", () => {
  it("sends the external-provider acknowledgement with a note-scoped draft request", async () => {
    const draftRequests = [];
    installFetchMock(
      baseRoutes({
        noteResponses: [
          apiResponse(
            voiceNote({
              metadata: { ...CAPTURE_METADATA, ...PROVENANCE, transcript_status: "ready" },
              transcribedText: "Provider transcript",
            })
          ),
        ],
        onPatch: () => {},
        extra: [
          {
            match: `/notes/${NOTE_ID}/graph-drafts`,
            method: "POST",
            response: (request) => {
              draftRequests.push(JSON.parse(request.init.body));
              return apiResponse({ change_set_id: "draft-2", status: "ready" }, 201);
            },
          },
        ],
      })
    );
    const { navigate } = renderDetail();
    await waitForLoadedVoiceNote();

    fireEvent.click(screen.getByLabelText(/I consent to send this note/));
    fireEvent.click(screen.getByRole("button", { name: "Draft graph update" }));

    await waitFor(() => expect(navigate).toHaveBeenCalledWith("/app/graph-drafts/draft-2"));
    expect(draftRequests).toEqual([
      { external_provider_acknowledged: true, mode: "graph_context" },
    ]);
  });
});

describe("NoteDetailCard Back", () => {
  function renderWithDepth(depth) {
    window.history.replaceState(
      depth ? { labTracker: { depth } } : null,
      "",
      `/app/notes/${NOTE_ID}`
    );
    installFetchMock(
      baseRoutes({
        noteResponses: apiResponse(
          voiceNote({ metadata: CAPTURE_METADATA, transcribedText: "Fly 12 climbed" })
        ),
        onPatch: () => {},
      })
    );
    return renderDetail();
  }

  it("Back uses in-app history with /app fallback", async () => {
    const back = vi.spyOn(window.history, "back").mockImplementation(() => {});

    const direct = renderWithDepth(0);
    fireEvent.click(await screen.findByRole("button", { name: "Back" }));
    expect(direct.navigate).toHaveBeenCalledWith("/app");
    expect(back).not.toHaveBeenCalled();
    cleanup();

    const fromApp = renderWithDepth(1);
    fireEvent.click(await screen.findByRole("button", { name: "Back" }));
    expect(back).toHaveBeenCalledTimes(1);
    expect(fromApp.navigate).not.toHaveBeenCalled();
  });
});
