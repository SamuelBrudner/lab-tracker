import * as React from "react";

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { vi } from "vitest";

import { ReviewPanel } from "./reviews.jsx";
import { apiResponse, binaryResponse, errorResponse, installFetchMock } from "../test/utils.js";

describe("ReviewPanel", () => {
  it("loads the pending queue and review detail", async () => {
    installFetchMock([
      {
        match: "/reviews/pending?limit=200&offset=0",
        response: apiResponse([
          {
            dataset_id: "dataset-1",
            requested_at: "2026-04-20T00:00:00Z",
            review_id: "review-1",
            status: "pending",
          },
        ]),
      },
      {
        match: "/datasets/dataset-1",
        response: apiResponse({
          commit_hash: "abc123",
          commit_manifest: {
            files: [],
            note_ids: ["note-1"],
          },
          dataset_id: "dataset-1",
          project_id: "project-1",
          question_links: [
            {
              outcome_status: "pending",
              question_id: "question-1",
              role: "primary",
            },
          ],
          status: "staged",
        }),
      },
      {
        match: "/datasets/dataset-1/files?limit=200&offset=0",
        response: apiResponse([
          {
            checksum: "sha256-1",
            file_id: "file-1",
            path: "rig/data.csv",
            size_bytes: 512,
          },
        ]),
      },
      {
        match: "/notes/note-1",
        response: apiResponse({
          created_at: "2026-04-20T02:00:00Z",
          note_id: "note-1",
          raw_content: "Microscope log",
          status: "committed",
          targets: [],
          transcribed_text: "",
        }),
      },
      {
        match:
          "/notes?project_id=project-1&target_entity_type=dataset&target_entity_id=dataset-1&limit=200&offset=0",
        response: apiResponse([
          {
            created_at: "2026-04-20T02:00:00Z",
            note_id: "note-1",
            raw_content: "Microscope log",
            status: "committed",
            targets: [
              {
                entity_id: "dataset-1",
                entity_type: "dataset",
              },
            ],
            transcribed_text: "",
          },
        ]),
      },
      {
        match: "/questions/question-1",
        response: apiResponse({
          question_id: "question-1",
          text: "Did the preparation stay stable?",
        }),
      },
    ]);

    render(
      <ReviewPanel
        token="token-1"
        user={{ role: "admin", username: "sam" }}
        projects={[{ name: "Project One", project_id: "project-1" }]}
        selectedProjectId="project-1"
        navigate={vi.fn()}
        onFlash={vi.fn()}
        onRefreshActiveProject={vi.fn(async () => ({ ok: true }))}
      />
    );

    expect(await screen.findByText("dataset-1")).toBeInTheDocument();
    expect(await screen.findByText("rig/data.csv")).toBeInTheDocument();
    expect(await screen.findByText("Did the preparation stay stable?")).toBeInTheDocument();
    expect(await screen.findByText("Microscope log")).toBeInTheDocument();
  });

  it("approves a review and refreshes the queue", async () => {
    const onFlash = vi.fn();
    const onRefreshActiveProject = vi.fn(async () => ({ ok: true }));

    installFetchMock([
      {
        match: "/reviews/pending?limit=200&offset=0",
        response: [
          apiResponse([
            {
              dataset_id: "dataset-2",
              requested_at: "2026-04-20T00:00:00Z",
              review_id: "review-2",
              status: "pending",
            },
          ]),
          apiResponse([]),
        ],
      },
      {
        match: "/datasets/dataset-2",
        response: apiResponse({
          commit_hash: "def456",
          commit_manifest: {
            files: [],
            note_ids: [],
          },
          dataset_id: "dataset-2",
          project_id: "project-1",
          question_links: [],
          status: "staged",
        }),
      },
      {
        match: "/datasets/dataset-2/files?limit=200&offset=0",
        response: apiResponse([]),
      },
      {
        match:
          "/notes?project_id=project-1&target_entity_type=dataset&target_entity_id=dataset-2&limit=200&offset=0",
        response: apiResponse([]),
      },
      {
        method: "PATCH",
        match: "/datasets/dataset-2/review",
        response: apiResponse({}),
      },
    ]);

    render(
      <ReviewPanel
        token="token-2"
        user={{ role: "editor", username: "sam" }}
        projects={[{ name: "Project One", project_id: "project-1" }]}
        selectedProjectId="project-1"
        navigate={vi.fn()}
        onFlash={onFlash}
        onRefreshActiveProject={onRefreshActiveProject}
      />
    );

    expect(await screen.findByText("dataset-2")).toBeInTheDocument();

    fireEvent.click(await screen.findByRole("button", { name: "Approve" }));

    expect(await screen.findByText("No pending reviews available.")).toBeInTheDocument();
    await waitFor(() => {
      expect(onFlash).toHaveBeenCalledWith("Dataset review approved.");
      expect(onRefreshActiveProject).toHaveBeenCalled();
    });
  });

  it("downloads protected dataset files with auth and reports failures", async () => {
    const onFlash = vi.fn();
    const onRefreshActiveProject = vi.fn(async () => ({ ok: true }));
    const clickSpy = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
    const createObjectURL = vi.fn(() => "blob:dataset-download");
    const revokeObjectURL = vi.fn();
    vi.stubGlobal("URL", Object.assign(URL, { createObjectURL, revokeObjectURL }));

    const fetchMock = installFetchMock([
      {
        match: "/reviews/pending?limit=200&offset=0",
        response: apiResponse([
          {
            dataset_id: "dataset-3",
            requested_at: "2026-04-20T00:00:00Z",
            review_id: "review-3",
            status: "pending",
          },
        ]),
      },
      {
        match: "/datasets/dataset-3",
        response: apiResponse({
          commit_hash: "ghi789",
          commit_manifest: {
            files: [],
            note_ids: [],
          },
          dataset_id: "dataset-3",
          project_id: "project-1",
          question_links: [],
          status: "staged",
        }),
      },
      {
        match: "/datasets/dataset-3/files?limit=200&offset=0",
        response: apiResponse([
          {
            checksum: "sha256-3",
            file_id: "file-3",
            path: "dataset.csv",
            size_bytes: 1024,
          },
        ]),
      },
      {
        match:
          "/notes?project_id=project-1&target_entity_type=dataset&target_entity_id=dataset-3&limit=200&offset=0",
        response: apiResponse([]),
      },
      {
        match: "/datasets/dataset-3/files/file-3/download",
        response: [
          binaryResponse({
            body: "downloaded-dataset",
            contentType: "text/csv",
            disposition: 'attachment; filename="dataset.csv"',
          }),
          errorResponse("Token has expired.", 401),
        ],
      },
    ]);

    render(
      <ReviewPanel
        token="token-3"
        user={{ role: "admin", username: "sam" }}
        projects={[{ name: "Project One", project_id: "project-1" }]}
        selectedProjectId="project-1"
        navigate={vi.fn()}
        onFlash={onFlash}
        onRefreshActiveProject={onRefreshActiveProject}
      />
    );

    fireEvent.click(await screen.findByRole("button", { name: "Download" }));

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        "/datasets/dataset-3/files/file-3/download",
        expect.objectContaining({
          headers: expect.objectContaining({
            Authorization: "Bearer token-3",
          }),
          method: "GET",
        })
      );
      expect(clickSpy).toHaveBeenCalled();
      expect(createObjectURL).toHaveBeenCalled();
    });

    fireEvent.click(screen.getByRole("button", { name: "Download" }));

    await waitFor(() => {
      expect(onFlash).toHaveBeenCalledWith("", "Token has expired.");
    });
    expect(revokeObjectURL).toHaveBeenCalledWith("blob:dataset-download");
  });
});
