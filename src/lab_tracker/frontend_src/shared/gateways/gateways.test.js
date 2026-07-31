import { describe, expect, it } from "vitest";

import { apiResponse, installFetchMock } from "../../test/utils.js";
import { ContractError } from "../contract.js";
import { auth, datasets, graphDrafts, notes, projects } from "./index.js";

const AUTH_USER = {
  created_at: "2026-07-20T00:00:00Z",
  role: "editor",
  user_id: "u-1",
  username: "sam",
};

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
          status: "drafting",
          operations: [{ operation_id: "op-1", status: "proposed", op: "create" }],
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
          user: AUTH_USER,
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

  it("validates bootstrap, current-user metadata, and refresh responses", async () => {
    installFetchMock([
      {
        match: "/auth/bootstrap-status",
        response: apiResponse({
          bootstrap_admin_configured: true,
          bootstrap_token: null,
          bootstrap_token_warning: null,
          first_admin_available: false,
          has_users: true,
        }),
      },
      {
        match: "/auth/me",
        response: apiResponse(AUTH_USER, 200, { auth_enabled: true }),
      },
      {
        match: "/auth/refresh",
        method: "POST",
        response: apiResponse({
          access_token: "refreshed",
          expires_at: "2026-07-21T00:00:00Z",
          token_type: "bearer",
          user: AUTH_USER,
        }),
      },
    ]);

    expect((await auth.getBootstrapStatus()).has_users).toBe(true);
    expect(await auth.getCurrentUser({ token: "tok" })).toEqual({
      authEnabled: true,
      user: AUTH_USER,
    });
    expect((await auth.refreshSession({ token: "tok" })).access_token).toBe(
      "refreshed"
    );
  });

  it("validates setup readiness without exposing provider credentials", async () => {
    installFetchMock([
      {
        match: "/auth/setup-readiness",
        response: apiResponse({
          background_worker_enabled: true,
          provider: "anthropic",
          provider_credential_configured: false,
          scheduler_enabled: true,
          source_revision: "0123456789abcdef0123456789abcdef01234567",
        }),
      },
    ]);

    await expect(auth.getSetupReadiness({ token: "tok" })).resolves.toEqual({
      background_worker_enabled: true,
      provider: "anthropic",
      provider_credential_configured: false,
      scheduler_enabled: true,
      source_revision: "0123456789abcdef0123456789abcdef01234567",
    });
  });

  it("rejects malformed bootstrap and current-user metadata", async () => {
    installFetchMock([
      {
        match: "/auth/bootstrap-status",
        response: apiResponse({ has_users: "yes" }),
      },
      {
        match: "/auth/me",
        response: apiResponse(AUTH_USER, 200, { auth_enabled: "yes" }),
      },
    ]);

    await expect(auth.getBootstrapStatus()).rejects.toBeInstanceOf(ContractError);
    await expect(auth.getCurrentUser()).rejects.toBeInstanceOf(ContractError);
  });

  it("validates user and invitation management responses", async () => {
    const invitation = {
      consumed_at: null,
      created_at: "2026-07-20T00:00:00Z",
      email: "sam@example.com",
      expires_at: "2026-07-21T00:00:00Z",
      invitation_id: "i-1",
      invite_url: "https://example.test/invite",
      mailto_url: "mailto:sam@example.com",
      revoked_at: null,
      role: "editor",
      status: "pending",
      warning: null,
    };
    installFetchMock([
      { match: /\/auth\/users\?limit=200/, response: apiResponse([AUTH_USER]) },
      {
        match: "/auth/users/u-1",
        method: "PATCH",
        response: apiResponse(AUTH_USER),
      },
      {
        match: /\/auth\/invitations\?limit=200/,
        response: apiResponse([invitation]),
      },
      {
        match: "/auth/invitations",
        method: "POST",
        response: apiResponse(invitation),
      },
      {
        match: "/auth/invitations/i-1",
        method: "DELETE",
        response: apiResponse({ ...invitation, revoked_at: "2026-07-20T01:00:00Z" }),
      },
    ]);

    expect((await auth.listUsers({ token: "tok" })).data).toEqual([AUTH_USER]);
    expect((await auth.updateUser("u-1", { role: "editor" }, { token: "tok" })).user_id).toBe(
      "u-1"
    );
    expect((await auth.listInvitations({ token: "tok" })).data[0].invitation_id).toBe(
      "i-1"
    );
    expect(
      (await auth.createInvitation({ email: invitation.email }, { token: "tok" }))
        .invitation_id
    ).toBe("i-1");
    expect((await auth.revokeInvitation("i-1", { token: "tok" })).revoked_at).toBeTruthy();
  });

  it("validates device enrollment and management responses", async () => {
    const device = {
      created_at: "2026-07-20T00:00:00Z",
      device_token_id: "d-1",
      label: "Phone",
      last_used_at: null,
      revoked_at: null,
    };
    const enrollment = {
      enrollment_id: "e-1",
      enrollment_qr_svg: "<svg></svg>",
      enrollment_url: "https://example.test/enroll",
      expires_at: "2026-07-20T01:00:00Z",
      offer_token: "offer",
    };
    const consumed = {
      created_at: "2026-07-20T00:00:00Z",
      device_token_id: "d-1",
      label: "Phone",
      secret: "secret",
    };
    installFetchMock([
      { match: "/auth/devices", response: apiResponse([device]) },
      {
        match: "/auth/devices/enrollment",
        method: "POST",
        response: apiResponse(enrollment),
      },
      {
        match: "/auth/devices/consume",
        method: "POST",
        response: apiResponse(consumed),
      },
      {
        match: "/auth/devices/d-1",
        method: "DELETE",
        response: apiResponse({ ...device, revoked_at: "2026-07-20T01:00:00Z" }),
      },
    ]);

    expect((await auth.listDevices({ token: "tok" })).data[0].label).toBe("Phone");
    expect((await auth.createDeviceEnrollment({}, { token: "tok" })).offer_token).toBe(
      "offer"
    );
    expect((await auth.consumeDeviceEnrollment({ offer_token: "offer", label: "Phone" })).secret).toBe(
      "secret"
    );
    expect((await auth.revokeDevice("d-1", { token: "tok" })).revoked_at).toBeTruthy();
  });

  it("validates personal access token responses", async () => {
    const personalToken = {
      created_at: "2026-07-20T00:00:00Z",
      expires_at: "2026-08-20T00:00:00Z",
      label: "Agent",
      last_used_at: null,
      read_only: true,
      revoked_at: null,
      role: "viewer",
      scope: "api",
      token_id: "t-1",
    };
    installFetchMock([
      { match: "/auth/tokens", response: apiResponse([personalToken]) },
      {
        match: "/auth/tokens",
        method: "POST",
        response: apiResponse({ ...personalToken, secret: "secret" }),
      },
      {
        match: "/auth/tokens/t-1",
        method: "DELETE",
        response: apiResponse({
          ...personalToken,
          revoked_at: "2026-07-20T01:00:00Z",
        }),
      },
    ]);

    expect((await auth.listPersonalAccessTokens({ token: "tok" })).data).toHaveLength(1);
    expect(
      (
        await auth.createPersonalAccessToken(
          { expires_at: personalToken.expires_at, label: "Agent" },
          { token: "tok" }
        )
      ).secret
    ).toBe("secret");
    expect(
      (await auth.revokePersonalAccessToken("t-1", { token: "tok" })).revoked_at
    ).toBeTruthy();
  });
});
