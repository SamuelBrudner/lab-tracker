import * as React from "react";

import { fireEvent, render, screen } from "@testing-library/react";
import { vi } from "vitest";

import { buildApiPath } from "../../shared/api.js";
import { apiResponse, installFetchMock } from "../../test/utils.js";
import { DatasetDetailCard } from "./DatasetDetailCard.jsx";

function requestedUrls(fetchMock) {
  return fetchMock.mock.calls.map(([input]) => (typeof input === "string" ? input : input.url));
}

describe("DatasetDetailCard", () => {
  it("loads a compact summary first and keeps legacy files and collection members lazy", async () => {
    const summaryPath = buildApiPath("/datasets/summaries", {
      dataset_id: "dataset-1",
      limit: 1,
      offset: 0,
    });
    const filesPath = buildApiPath("/datasets/dataset-1/manifest-files", {
      limit: 100,
      offset: 0,
    });
    const experimentPath = buildApiPath("/datasets/dataset-1/experiments", {
      limit: 50,
      offset: 0,
    });
    const fetchMock = installFetchMock([
      {
        match: summaryPath,
        response: apiResponse(
          [
            {
              collection_snapshots: [
                {
                  collection_id: "collection-1",
                  collection_key: "rig-output",
                  manifest_hash: "c".repeat(64),
                  member_count: 10000,
                  observed_at: "2026-07-24T12:00:00Z",
                  snapshot_id: "snapshot-1",
                  total_size_bytes: 4096,
                },
              ],
              commit_hash: "commit-1",
              created_at: "2026-07-24T12:00:00Z",
              dataset_id: "dataset-1",
              project_id: "project-1",
              question_links: [
                {
                  outcome_status: "unknown",
                  question_id: "question-1",
                  role: "primary",
                },
              ],
              status: "committed",
              updated_at: "2026-07-24T12:00:00Z",
            },
          ],
          200,
          { limit: 1, offset: 0, total: 1 }
        ),
      },
      {
        match: experimentPath,
        response: apiResponse([], 200, { limit: 50, offset: 0, total: 0 }),
      },
      {
        match: filesPath,
        response: apiResponse(
          [
            {
              checksum: "a".repeat(64),
              file_id: "file-1",
              path: "legacy.bin",
              size_bytes: 128,
            },
          ],
          200,
          { limit: 100, offset: 0, total: 1 }
        ),
      },
    ]);

    render(
      <DatasetDetailCard
        token="token-1"
        datasetId="dataset-1"
        projects={[{ name: "Project One", project_id: "project-1" }]}
        navigate={vi.fn()}
        onSetActiveProject={vi.fn()}
      />
    );

    expect(await screen.findByText("10000 members")).toBeInTheDocument();
    const initialUrls = requestedUrls(fetchMock);
    expect(initialUrls).toContain(summaryPath);
    expect(initialUrls).not.toContain("/datasets/dataset-1");
    expect(initialUrls).not.toContain(filesPath);
    expect(
      initialUrls.some((url) => url.includes("/collection-snapshots/snapshot-1/members"))
    ).toBe(false);

    fireEvent.click(screen.getByRole("button", { name: "Show legacy files" }));
    expect(await screen.findByText("legacy.bin")).toBeInTheDocument();
    expect(requestedUrls(fetchMock)).toContain(filesPath);
  });
});
