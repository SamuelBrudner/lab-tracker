import * as React from "react";

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { vi } from "vitest";

import { QuestionExtractionInboxPanel } from "./questions.jsx";
import { binaryResponse, errorResponse, installFetchMock } from "../test/utils.js";

describe("QuestionExtractionInboxPanel", () => {
  it("downloads protected note raw assets with auth", async () => {
    const onFlash = vi.fn();
    const clickSpy = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
    const createObjectURL = vi.fn(() => "blob:note-download");
    const revokeObjectURL = vi.fn();
    vi.stubGlobal("URL", Object.assign(URL, { createObjectURL, revokeObjectURL }));

    const fetchMock = installFetchMock([
      {
        match: "/notes/note-1/raw",
        response: binaryResponse({
          body: "raw-capture",
          contentType: "text/plain",
          disposition: 'attachment; filename="capture.txt"',
        }),
      },
    ]);

    render(
      <QuestionExtractionInboxPanel
        canWrite
        busy={false}
        token="token-1"
        selectedProjectId="project-1"
        notes={[]}
        questions={[]}
        navigate={vi.fn()}
        selectedNoteId="note-1"
        onSelectedNoteIdChange={vi.fn()}
        note={{
          created_at: "2026-04-20T00:00:00Z",
          note_id: "note-1",
          status: "staged",
          transcribed_text: "typed capture",
        }}
        noteRaw={{
          content_base64: "cmF3LWNhcHR1cmU=",
          content_type: "text/plain",
          filename: "capture.txt",
          size_bytes: 11,
        }}
        noteRawError=""
        candidates={[]}
        onExtractCandidates={vi.fn()}
        onUpdateCandidate={vi.fn()}
        onToggleCandidateSelected={vi.fn()}
        onSelectAllPending={vi.fn()}
        onClearSelection={vi.fn()}
        onRejectSelected={vi.fn()}
        onStageSelected={vi.fn()}
        onFlash={onFlash}
      />
    );

    fireEvent.click(screen.getByRole("button", { name: "Download" }));

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        "/notes/note-1/raw",
        expect.objectContaining({
          headers: expect.objectContaining({
            Authorization: "Bearer token-1",
          }),
          method: "GET",
        })
      );
      expect(clickSpy).toHaveBeenCalled();
      expect(createObjectURL).toHaveBeenCalled();
      expect(onFlash).not.toHaveBeenCalled();
    });
    expect(revokeObjectURL).toHaveBeenCalledWith("blob:note-download");
  });

  it("surfaces download errors through flash messaging", async () => {
    const onFlash = vi.fn();

    installFetchMock([
      {
        match: "/notes/note-1/raw",
        response: errorResponse("Token has expired.", 401),
      },
    ]);

    render(
      <QuestionExtractionInboxPanel
        canWrite
        busy={false}
        token="expired-token"
        selectedProjectId="project-1"
        notes={[]}
        questions={[]}
        navigate={vi.fn()}
        selectedNoteId="note-1"
        onSelectedNoteIdChange={vi.fn()}
        note={{
          created_at: "2026-04-20T00:00:00Z",
          note_id: "note-1",
          status: "staged",
          transcribed_text: "typed capture",
        }}
        noteRaw={{
          content_base64: "cmF3LWNhcHR1cmU=",
          content_type: "text/plain",
          filename: "capture.txt",
          size_bytes: 11,
        }}
        noteRawError=""
        candidates={[]}
        onExtractCandidates={vi.fn()}
        onUpdateCandidate={vi.fn()}
        onToggleCandidateSelected={vi.fn()}
        onSelectAllPending={vi.fn()}
        onClearSelection={vi.fn()}
        onRejectSelected={vi.fn()}
        onStageSelected={vi.fn()}
        onFlash={onFlash}
      />
    );

    fireEvent.click(screen.getByRole("button", { name: "Download" }));

    await waitFor(() => {
      expect(onFlash).toHaveBeenCalledWith("", "Token has expired.");
    });
  });
});
