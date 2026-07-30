import * as React from "react";

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import {
  apiResponse,
  installFetchMock as installBaseFetchMock,
} from "../test/utils.js";
import { AgentAccessPage } from "./agent-access.jsx";

const TOKEN_ID = "22222222-2222-4222-8222-222222222222";
const SOURCE_REVISION = "0123456789abcdef0123456789abcdef01234567";
const PROJECT = {
  name: "Deerhake lab",
  project_id: "11111111-1111-4111-8111-111111111111",
};

function installFetchMock(routes, readinessOverrides = {}) {
  return installBaseFetchMock([
    {
      match: "/auth/setup-readiness",
      response: apiResponse({
        background_worker_enabled: true,
        provider: "openai",
        provider_credential_configured: true,
        scheduler_enabled: true,
        source_revision: SOURCE_REVISION,
        ...readinessOverrides,
      }),
    },
    ...routes,
  ]);
}

function issuedTokenPayload(overrides = {}) {
  return {
    created_at: "2026-07-07T12:00:00Z",
    expires_at: "2026-08-06T12:00:00Z",
    label: "Coding agent",
    last_used_at: null,
    read_only: true,
    revoked_at: null,
    role: "viewer",
    scope: "api",
    token_id: TOKEN_ID,
    ...overrides,
  };
}

function renderPage(props = {}) {
  return render(
    <AgentAccessPage
      token="user-token"
      user={{ role: "admin", username: "sam" }}
      authEnabled
      selectedProject={PROJECT}
      navigate={vi.fn()}
      setBusy={vi.fn()}
      setFlash={vi.fn()}
      {...props}
    />
  );
}

describe("AgentAccessPage", () => {
  it("mints a run-due-scoped token for the scheduler-trigger level", async () => {
    let mintBody = null;
    installFetchMock([
      {
        match: "/auth/tokens",
        response: [
          apiResponse([], 200, { limit: 1, offset: 0, total: 0 }),
          apiResponse([issuedTokenPayload()], 200, { limit: 1, offset: 0, total: 1 }),
        ],
      },
      {
        match: "/auth/tokens",
        method: "POST",
        response: (request) => {
          mintBody = JSON.parse(request.init.body);
          return apiResponse(
            issuedTokenPayload({
              label: mintBody.label,
              read_only: mintBody.read_only,
              role: mintBody.role,
              secret: "lpat_test-secret",
            }),
            201
          );
        },
      },
    ]);

    renderPage({ setFlash: vi.fn() });

    await waitFor(() => expect(screen.getByText("No agent tokens yet.")).toBeInTheDocument());

    fireEvent.change(screen.getByLabelText("Label"), { target: { value: "Cron" } });
    fireEvent.change(screen.getByLabelText("Access"), { target: { value: "scheduler" } });
    fireEvent.click(screen.getByRole("button", { name: "Create agent token" }));

    await waitFor(() => expect(mintBody).not.toBeNull());
    expect(mintBody.role).toBe("admin");
    expect(mintBody.scope).toBe("batch_run_due");
  });

  it("mints a token and shows one-time setup commands", async () => {
    const setFlash = vi.fn();
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(window.navigator, "clipboard", {
      configurable: true,
      value: { writeText },
    });
    let mintBody = null;
    const fetchMock = installFetchMock([
      {
        match: "/auth/tokens",
        response: [
          apiResponse([], 200, { limit: 1, offset: 0, total: 0 }),
          apiResponse([issuedTokenPayload()], 200, { limit: 1, offset: 0, total: 1 }),
        ],
      },
      {
        match: "/auth/tokens",
        method: "POST",
        response: (request) => {
          mintBody = JSON.parse(request.init.body);
          return apiResponse(
            issuedTokenPayload({
              label: mintBody.label,
              read_only: mintBody.read_only,
              role: mintBody.role,
              secret: "lpat_test-secret",
            }),
            201
          );
        },
      },
    ]);

    renderPage({ setFlash });

    await waitFor(() => expect(screen.getByText("No agent tokens yet.")).toBeInTheDocument());

    fireEvent.change(screen.getByLabelText("Label"), {
      target: { value: "Laptop agent" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Create agent token" }));

    await waitFor(() =>
      expect(setFlash).toHaveBeenCalledWith(
        'Token "Laptop agent" created. It is shown only once — copy it now.'
      )
    );

    expect(mintBody.label).toBe("Laptop agent");
    expect(mintBody.role).toBe("editor");
    expect(mintBody.read_only).toBe(false);
    expect(mintBody.scope).toBe("all");
    const deltaDays = (new Date(mintBody.expires_at).getTime() - Date.now()) / 86400000;
    expect(deltaDays).toBeGreaterThan(29);
    expect(deltaDays).toBeLessThanOrEqual(30);

    expect(screen.getByText("lpat_test-secret")).toBeInTheDocument();
    const origin = window.location.origin;
    const commandBlocks = document.querySelectorAll(".command-block");
    const commandText = Array.from(commandBlocks)
      .map((node) => node.textContent)
      .join("\n");
    expect(commandText).toContain(
      `lt setup connect --base-url ${origin} --project ${PROJECT.project_id} ` +
        "--save-token --yes"
    );
    expect(commandText).not.toContain("lpat_test-secret");
    expect(commandText).toContain(
      'Read-Host "Lab Tracker one-time token" -AsSecureString'
    );
    expect(commandText).toContain("stty -echo < /dev/tty");
    expect(commandText).toContain(
      "IFS= read -r LAB_TRACKER_ACCESS_TOKEN < /dev/tty"
    );
    expect(commandText).toContain(
      "Remove-Item Env:LAB_TRACKER_ACCESS_TOKEN -ErrorAction SilentlyContinue"
    );
    expect(commandText).toContain("unset LAB_TRACKER_ACCESS_TOKEN");
    expect(commandText).toContain("try {");
    expect(commandText).toContain("} finally {");
    expect(commandText).not.toContain("LAB_TRACKER_MCP_API_KEY");

    const snippets = Array.from(document.querySelectorAll(".command-snippet"));
    for (const title of ["Windows (PowerShell)", "macOS / Linux"]) {
      const snippet = snippets.find(
        (node) => node.querySelector("strong")?.textContent === title
      );
      expect(snippet).toBeDefined();
      fireEvent.click(snippet.querySelector("button"));
    }
    await waitFor(() => expect(writeText).toHaveBeenCalledTimes(2));
    for (const [copiedSetupBlock] of writeText.mock.calls) {
      expect(copiedSetupBlock).not.toContain("lpat_test-secret");
      expect(copiedSetupBlock).toContain("--save-token --yes");
    }

    expect(commandText).toContain("lt setup init --install-skills --dry-run");
    expect(commandText).toContain("lt setup init --install-skills --yes");
    expect(commandText).toContain(
      `lt project bind --project-id ${PROJECT.project_id} --dry-run`
    );
    expect(commandText).toContain(
      `lt hooks install --project ${PROJECT.project_id} --yes`
    );
    expect(commandText).toContain("lt setup status");
    expect(commandText).toContain("codex mcp add lab-tracker -- lt-mcp");
    expect(commandText).toContain(
      `lt setup verify-mcp --expected-revision ${SOURCE_REVISION}`
    );
    expect(commandText).toContain("codex mcp list");
    const applyingBlocks = Array.from(commandBlocks).filter(
      (node) =>
        node.textContent.includes("lt setup init") ||
        node.textContent.includes("lt project bind") ||
        node.textContent.includes("codex mcp")
    );
    expect(applyingBlocks.every((node) => !node.textContent.includes("\n"))).toBe(true);
    expect(document.body.textContent).toContain(
      `uv tool install --force "lab-tracker @ git+https://github.com/` +
        `SamuelBrudner/lab-tracker.git@${SOURCE_REVISION}"`
    );
    expect(commandText).toContain("import lab_tracker_client");
    expect(document.body.textContent).toContain(
      "used by both lt and lt-mcp"
    );
    expect(document.body.textContent).toContain(
      "paste the token at its hidden prompt"
    );
    expect(document.body.textContent).toContain("both Claude and Codex user homes");
    expect(document.body.textContent).toContain(
      "Server-side AI drafting uses the Lab Tracker operator"
    );
    // The known selected project is bound directly; setup never guesses a name
    // or creates a duplicate.
    expect(commandText).not.toContain("--create");
    expect(commandText).not.toContain("My project");

    expect(fetchMock).toHaveBeenCalledWith(
      "/auth/tokens",
      expect.objectContaining({
        headers: expect.objectContaining({ Authorization: "Bearer user-token" }),
        method: "POST",
      })
    );
  });

  it("uses the exact selected project and offers hooks for a staging token", async () => {
    installFetchMock([
      {
        match: "/auth/tokens",
        response: apiResponse([], 200, { limit: 1, offset: 0, total: 0 }),
      },
      {
        match: "/auth/tokens",
        method: "POST",
        response: (request) => {
          const body = JSON.parse(request.init.body);
          expect(body.role).toBe("editor");
          expect(body.read_only).toBe(false);
          return apiResponse(
            issuedTokenPayload({
              read_only: false,
              role: "editor",
              secret: "lpat_write-secret",
            }),
            201
          );
        },
      },
    ]);

    renderPage();

    await waitFor(() => expect(screen.getByText("No agent tokens yet.")).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: "Create agent token" }));

    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Copy token" })).toBeInTheDocument()
    );
    const commandText = Array.from(document.querySelectorAll(".command-block"))
      .map((node) => node.textContent)
      .join("\n");
    expect(commandText).toContain(
      `lt project bind --project-id ${PROJECT.project_id} --yes`
    );
    expect(commandText).toContain(
      `lt hooks install --project ${PROJECT.project_id} --yes`
    );
    expect(commandText).not.toContain("--create");
  });

  it("disables the submit button while a mint is in flight", async () => {
    installFetchMock([
      {
        match: "/auth/tokens",
        response: apiResponse([], 200, { limit: 1, offset: 0, total: 0 }),
      },
      {
        match: "/auth/tokens",
        method: "POST",
        response: () => new Promise(() => {}),
      },
    ]);

    renderPage();

    await waitFor(() => expect(screen.getByText("No agent tokens yet.")).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: "Create agent token" }));

    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Creating…" })).toBeDisabled()
    );
  });

  it("offers only the read-only level to viewers", async () => {
    installFetchMock([
      {
        match: "/auth/tokens",
        response: apiResponse([], 200, { limit: 1, offset: 0, total: 0 }),
      },
    ]);

    renderPage({ user: { role: "viewer", username: "vi" } });

    await waitFor(() => expect(screen.getByText("No agent tokens yet.")).toBeInTheDocument());
    const options = screen.getByLabelText("Access").querySelectorAll("option");
    expect(Array.from(options).map((option) => option.value)).toEqual(["read"]);
  });

  it("offers all levels to admins", async () => {
    installFetchMock([
      {
        match: "/auth/tokens",
        response: apiResponse([], 200, { limit: 1, offset: 0, total: 0 }),
      },
    ]);

    renderPage();

    await waitFor(() => expect(screen.getByText("No agent tokens yet.")).toBeInTheDocument());
    const options = screen.getByLabelText("Access").querySelectorAll("option");
    expect(Array.from(options).map((option) => option.value)).toEqual([
      "stage",
      "read",
      "scheduler",
    ]);
  });

  it("revokes a token", async () => {
    const setFlash = vi.fn();
    const fetchMock = installFetchMock([
      {
        match: "/auth/tokens",
        response: [
          apiResponse([issuedTokenPayload()], 200, { limit: 1, offset: 0, total: 1 }),
          apiResponse([issuedTokenPayload({ revoked_at: "2026-07-07T13:00:00Z" })], 200, {
            limit: 1,
            offset: 0,
            total: 1,
          }),
        ],
      },
      {
        match: `/auth/tokens/${TOKEN_ID}`,
        method: "DELETE",
        response: apiResponse(issuedTokenPayload({ revoked_at: "2026-07-07T13:00:00Z" })),
      },
    ]);

    renderPage({ setFlash });

    await waitFor(() => expect(screen.getByText("Coding agent")).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: "Revoke" }));

    await waitFor(() => expect(setFlash).toHaveBeenCalledWith("Token revoked."));
    expect(fetchMock).toHaveBeenCalledWith(
      `/auth/tokens/${TOKEN_ID}`,
      expect.objectContaining({ method: "DELETE" })
    );
  });

  it("copies the minted secret", async () => {
    const setFlash = vi.fn();
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(window.navigator, "clipboard", {
      configurable: true,
      value: { writeText },
    });
    installFetchMock([
      {
        match: "/auth/tokens",
        response: apiResponse([], 200, { limit: 1, offset: 0, total: 0 }),
      },
      {
        match: "/auth/tokens",
        method: "POST",
        response: apiResponse(issuedTokenPayload({ secret: "lpat_copy-me" }), 201),
      },
    ]);

    renderPage({ setFlash });

    await waitFor(() => expect(screen.getByText("No agent tokens yet.")).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: "Create agent token" }));
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Copy token" })).toBeInTheDocument()
    );

    fireEvent.click(screen.getByRole("button", { name: "Copy token" }));
    await waitFor(() => expect(setFlash).toHaveBeenCalledWith("Token copied."));
    expect(writeText).toHaveBeenCalledWith("lpat_copy-me");
  });

  it("shows tokenless setup commands when auth is disabled", async () => {
    installFetchMock([]);

    renderPage({ authEnabled: false });

    expect(
      screen.getByText("Authentication is disabled on this server")
    ).toBeInTheDocument();
    await screen.findByText(
      `Install matching lt and lt-mcp (${SOURCE_REVISION.slice(0, 12)})`
    );
    const commandText = Array.from(document.querySelectorAll(".command-block"))
      .map((node) => node.textContent)
      .join("\n");
    expect(commandText).toContain(
      `lt setup connect --base-url ${window.location.origin} --project ` +
        `${PROJECT.project_id} --yes`
    );
    expect(commandText).not.toContain("LAB_TRACKER_ACCESS_TOKEN");
    expect(commandText).toContain("lt setup init --install-skills --dry-run");
    expect(commandText).toContain("lt setup init --install-skills --yes");
    expect(commandText).toContain(
      `lt project bind --project-id ${PROJECT.project_id} --yes`
    );
    expect(commandText).toContain(
      `lt hooks install --project ${PROJECT.project_id} --yes`
    );
    expect(commandText).toContain("lt setup status");
    expect(commandText).toContain("codex mcp add lab-tracker -- lt-mcp");
    expect(commandText).toContain(
      `lt setup verify-mcp --expected-revision ${SOURCE_REVISION}`
    );
    expect(commandText).toContain("codex mcp list");
    expect(document.body.textContent).toContain(
      `uv tool install --force "lab-tracker @ git+https://github.com/` +
        `SamuelBrudner/lab-tracker.git@${SOURCE_REVISION}"`
    );
    expect(document.body.textContent).toContain("Claude and Codex user skill homes");
    expect(document.body.textContent).toContain("checks health");
    expect(screen.queryByRole("button", { name: "Create agent token" })).toBeNull();
  });

  it("withholds every local setup command for an unpinned deployment", async () => {
    installFetchMock([], { source_revision: "unknown" });

    renderPage({ authEnabled: false });

    expect(
      await screen.findByText(/Matching client installation is unavailable/)
    ).toBeInTheDocument();
    expect(
      screen.getByText(/Local connection and repository commands are withheld/)
    ).toBeInTheDocument();
    const commandText = Array.from(document.querySelectorAll(".command-block"))
      .map((node) => node.textContent)
      .join("\n");
    expect(commandText).not.toContain("uv tool install");
    expect(commandText).not.toContain("lt setup connect");
    expect(commandText).not.toContain("lt setup init");
    expect(commandText).not.toContain("lt project bind");
    expect(commandText).not.toContain("lt hooks install");
    expect(commandText).not.toContain("codex mcp add");
  });
});
