import * as React from "react";

import { fireEvent, render, screen, waitFor } from "@testing-library/react";

import { buildApiPath } from "../../shared/api.js";
import { apiResponse, binaryResponse, installFetchMock } from "../../test/utils.js";
import { CollectionSnapshotBrowser } from "./CollectionSnapshotBrowser.jsx";

function requestedUrls(fetchMock) {
  return fetchMock.mock.calls.map(([input]) => (typeof input === "string" ? input : input.url));
}

describe("CollectionSnapshotBrowser", () => {
  it("does not request members before expansion and pages exactly 100 at a time", async () => {
    const firstPath = buildApiPath("/collection-snapshots/snapshot-1/members", {
      limit: 100,
      offset: 0,
    });
    const secondPath = buildApiPath("/collection-snapshots/snapshot-1/members", {
      limit: 100,
      offset: 100,
    });
    const firstPageMembers = Array.from({ length: 100 }, (_, index) => ({
      checksum: "a".repeat(64),
      path: `trial-${String(index + 1).padStart(4, "0")}/data.bin`,
      size_bytes: 512 + index,
    }));
    const fetchMock = installFetchMock([
      {
        match: firstPath,
        response: apiResponse(
          firstPageMembers,
          200,
          { limit: 100, offset: 0, total: 101 }
        ),
      },
      {
        match: secondPath,
        response: apiResponse(
          [
            {
              checksum: "b".repeat(64),
              path: "trial-0101/data.bin",
              size_bytes: 1024,
            },
          ],
          200,
          { limit: 100, offset: 100, total: 101 }
        ),
      },
    ]);

    render(
      <CollectionSnapshotBrowser
        token="token-1"
        snapshotId="snapshot-1"
        memberCount={101}
      />
    );

    expect(fetchMock).not.toHaveBeenCalled();
    expect(screen.queryByText("trial-0001/data.bin")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Browse members" }));

    expect(await screen.findByText("trial-0001/data.bin")).toBeInTheDocument();
    expect(requestedUrls(fetchMock)).toEqual([firstPath]);
    expect(screen.getByText("1-100 of 101")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Next members" }));

    expect(await screen.findByText("trial-0101/data.bin")).toBeInTheDocument();
    expect(screen.queryByText("trial-0001/data.bin")).not.toBeInTheDocument();
    expect(requestedUrls(fetchMock)).toEqual([firstPath, secondPath]);
  });

  it("runs bounded path searches and reports an empty result", async () => {
    const firstPath = buildApiPath("/collection-snapshots/snapshot-1/members", {
      limit: 100,
      offset: 0,
    });
    const searchPath = buildApiPath("/collection-snapshots/snapshot-1/members", {
      limit: 100,
      offset: 0,
      q: "missing-trial",
    });
    const fetchMock = installFetchMock([
      {
        match: firstPath,
        response: apiResponse([], 200, { limit: 100, offset: 0, total: 0 }),
      },
      {
        match: searchPath,
        response: apiResponse([], 200, { limit: 100, offset: 0, total: 0 }),
      },
    ]);

    render(
      <CollectionSnapshotBrowser token="token-1" snapshotId="snapshot-1" />
    );
    fireEvent.click(screen.getByRole("button", { name: "Browse members" }));
    await screen.findByText("(no members)");

    fireEvent.change(screen.getByLabelText("Member path search"), {
      target: { value: "missing-trial" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Search" }));

    expect(await screen.findByText("No members match this path.")).toBeInTheDocument();
    expect(requestedUrls(fetchMock)).toEqual([firstPath, searchPath]);
  });

  it("downloads a manifest only on explicit request", async () => {
    const createObjectUrl = vi.fn(() => "blob:manifest");
    const revokeObjectUrl = vi.fn();
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
    vi.stubGlobal("URL", {
      ...URL,
      createObjectURL: createObjectUrl,
      revokeObjectURL: revokeObjectUrl,
    });
    const fetchMock = installFetchMock([
      {
        match: "/collection-snapshots/snapshot-1/manifest",
        response: binaryResponse({
          body: "{}",
          contentType: "application/json",
          disposition: 'attachment; filename="server-manifest.json"',
        }),
      },
    ]);

    render(
      <CollectionSnapshotBrowser
        token="token-1"
        snapshotId="snapshot-1"
        manifestFilename="run-manifest.json"
      />
    );

    expect(fetchMock).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Download manifest" }));

    await waitFor(() => expect(createObjectUrl).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(revokeObjectUrl).toHaveBeenCalledWith("blob:manifest"));
    expect(requestedUrls(fetchMock)).toEqual([
      "/collection-snapshots/snapshot-1/manifest",
    ]);
    vi.unstubAllGlobals();
  });
});
