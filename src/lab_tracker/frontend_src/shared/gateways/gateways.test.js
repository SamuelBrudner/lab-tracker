import { describe, expect, it } from "vitest";

import { apiResponse, installFetchMock } from "../../test/utils.js";
import { ContractError } from "../contract.js";
import { auth, datasets, graphDrafts, notes, projects } from "./index.js";

describe("projects gateway", () => {
  it("listMembers returns validated members", async () => {
    installFetchMock([
      {
        match: /\/projects\/p-1\/members/,
        response: apiResponse([{ user_id: "u-1", role: "owner" }]),
      },
    ]);
    const { data } = await projects.listMembers("p-1", { token: "t" });
    expect(data).toEqual([{ user_id: "u-1", role: "owner" }]);
  });

  it("listMembers fails loudly when the collection data is not an array", async () => {
    installFetchMock([
      { match: /\/projects\/p-1\/members/, response: apiResponse({ user_id: "u-1" }) },
    ]);
    await expect(projects.listMembers("p-1", { token: "t" })).rejects.toBeInstanceOf(ContractError);
  });

  it("listProjects validates each project item", async () => {
    installFetchMock([{ match: /\/projects\?/, response: apiResponse([{ project_id: "p-1", name: "One" }]) }]);
    expect(await projects.listProjects({ token: "t" })).toEqual([{ project_id: "p-1", name: "One" }]);
  });

  it("listProjects rejects a project missing its identity", async () => {
    installFetchMock([{ match: /\/projects\?/, response: apiResponse([{ name: "no id" }]) }]);
    await expect(projects.listProjects({ token: "t" })).rejects.toBeInstanceOf(ContractError);
  });
});

describe("datasets gateway", () => {
  it("getDataset returns the validated dataset", async () => {
    installFetchMock([
      { match: "/datasets/ds-1", response: apiResponse({ dataset_id: "ds-1", status: "committed" }) },
    ]);
    const dataset = await datasets.getDataset("ds-1", { token: "t" });
    expect(dataset.dataset_id).toBe("ds-1");
  });

  it("getDataset throws when data is missing the dataset identity", async () => {
    installFetchMock([{ match: "/datasets/ds-1", response: apiResponse({ status: "committed" }) }]);
    await expect(datasets.getDataset("ds-1", { token: "t" })).rejects.toBeInstanceOf(ContractError);
  });

  it("getDataset throws when data is null rather than an object", async () => {
    installFetchMock([{ match: "/datasets/ds-1", response: apiResponse(null) }]);
    // apiResponse(null) carries a data key whose value is null; a null dataset
    // is not an object, so validation fails loudly instead of returning null.
    await expect(datasets.getDataset("ds-1", { token: "t" })).rejects.toBeInstanceOf(ContractError);
  });
});

describe("notes gateway", () => {
  it("getNote accepts a binary note with a raw_asset", async () => {
    installFetchMock([
      {
        match: "/notes/n-1",
        response: apiResponse({
          note_id: "n-1",
          status: "committed",
          raw_asset: { content_type: "image/png", filename: "x.png" },
          metadata: { transcript_status: "ready" },
        }),
      },
    ]);
    const note = await notes.getNote("n-1", { token: "t" });
    expect(note.note_id).toBe("n-1");
    expect(note.raw_asset.content_type).toBe("image/png");
  });

  it("getNote accepts a text note with null raw_asset", async () => {
    installFetchMock([
      { match: "/notes/n-1", response: apiResponse({ note_id: "n-1", raw_asset: null, raw_content: "hi" }) },
    ]);
    const note = await notes.getNote("n-1", { token: "t" });
    expect(note.raw_asset).toBeNull();
  });

  it("getNote rejects a note missing its identity", async () => {
    installFetchMock([{ match: "/notes/n-1", response: apiResponse({ status: "committed" }) }]);
    await expect(notes.getNote("n-1", { token: "t" })).rejects.toBeInstanceOf(ContractError);
  });
});

describe("graph-drafts gateway", () => {
  it("getChangeSet validates the change set and its operations", async () => {
    installFetchMock([
      {
        match: "/graph-drafts/cs-1",
        response: apiResponse({
          change_set_id: "cs-1",
          status: "draft",
          operations: [{ operation_id: "op-1", status: "pending", op: "create" }],
        }),
      },
    ]);
    const changeSet = await graphDrafts.getChangeSet("cs-1", { token: "t" });
    expect(changeSet.operations).toHaveLength(1);
  });

  it("getChangeSet fails loudly when operations drifted to a non-array", async () => {
    installFetchMock([
      { match: "/graph-drafts/cs-1", response: apiResponse({ change_set_id: "cs-1", operations: "nope" }) },
    ]);
    await expect(graphDrafts.getChangeSet("cs-1", { token: "t" })).rejects.toBeInstanceOf(ContractError);
  });

  it("getChangeSet rejects an operation missing its identity", async () => {
    installFetchMock([
      {
        match: "/graph-drafts/cs-1",
        response: apiResponse({ change_set_id: "cs-1", operations: [{ op: "create" }] }),
      },
    ]);
    await expect(graphDrafts.getChangeSet("cs-1", { token: "t" })).rejects.toBeInstanceOf(ContractError);
  });
});

describe("auth gateway", () => {
  it("authenticate posts credentials and validates the token payload", async () => {
    installFetchMock([
      {
        match: "/auth/login",
        method: "POST",
        response: apiResponse({
          access_token: "tok",
          token_type: "bearer",
          expires_at: "2026-07-20T00:00:00Z",
          user: { user_id: "u-1", username: "sam", role: "editor" },
        }),
      },
    ]);
    const payload = await auth.authenticate("/auth/login", { username: "sam", password: "pw" });
    expect(payload.access_token).toBe("tok");
    expect(payload.user.user_id).toBe("u-1");
  });

  it("authenticate throws when the token payload has no user", async () => {
    installFetchMock([
      { match: "/auth/login", method: "POST", response: apiResponse({ access_token: "tok" }) },
    ]);
    await expect(
      auth.authenticate("/auth/login", { username: "sam", password: "pw" })
    ).rejects.toBeInstanceOf(ContractError);
  });
});
