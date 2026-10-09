import * as React from "react";

import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { apiResponse, errorResponse, installFetchMock } from "../test/utils.js";
import { OnboardingPage } from "./onboarding.jsx";

const USER = {
  role: "admin",
  user_id: "user-1",
  username: "marion.deerhake@yale.edu",
};
const SOURCE_REVISION = "0123456789abcdef0123456789abcdef01234567";

const PROJECT = {
  description: "Track the lab's first research program.",
  name: "Deerhake lab",
  project_id: "project-1",
};

const READY_RUNTIME = {
  background_worker_enabled: true,
  provider: "openai",
  provider_credential_configured: true,
  scheduler_enabled: true,
  source_revision: SOURCE_REVISION,
};

function renderPage(props = {}) {
  return render(
    <OnboardingPage
      token="user-token"
      user={USER}
      projects={[]}
      selectedProjectId=""
      setSelectedProjectId={vi.fn()}
      refreshProjects={vi.fn().mockResolvedValue([])}
      canWrite
      canManageSchedule
      navigate={vi.fn()}
      setBusy={vi.fn()}
      setFlash={vi.fn()}
      {...props}
    />
  );
}

// Without an immutable source revision, step 5 shows only its withheld warning: no
// client list, intro, /mcp hint, or organization-policy note. The step used to show
// the intro and the /mcp note even then; this pins the deliberate change.
function expectClientStepWithheld() {
  const step = screen
    .getByRole("heading", { name: "Connect your coding assistant" })
    .closest("li");
  expect(
    within(step).getByText(/MCP verification is blocked until the server reports/)
  ).toHaveClass("warn");
  expect(step.querySelectorAll(".subtle")).toHaveLength(0);
  expect(step.textContent).not.toContain("/mcp");
  expect(step.textContent).not.toContain("administrator may need to allow this server");
  expect(step.textContent).not.toContain("Claude Desktop chat");
  expect(step.textContent).not.toContain("Codex in the ChatGPT desktop app");
  expect(within(step).queryByRole("link")).toBeNull();
}

describe("OnboardingPage", () => {
  it.each([
    {currency: "superseded", overdue: false, label: "Upgrade available", recommend: true},
    {currency: "recommended", overdue: true, label: "Review overdue", recommend: false},
    {currency: "custom_endpoint", overdue: false, label: "Needs model review", recommend: false},
  ])("shows active model currency: $label", async ({currency, overdue, label, recommend}) => {
    installFetchMock([
      {
        match: "/auth/setup-readiness",
        response: apiResponse({
          ...READY_RUNTIME,
          ai_models: [{
            provider: "openai",
            setting: "LAB_TRACKER_OPENAI_MODEL",
            workloads: ["note_graph_draft", "daily_review"],
            active: true,
            configured_model: "gpt-4o-mini",
            recommended_model: "gpt-6.1-sol",
            currency,
            reviewed_on: "2026-10-08",
            review_due_on: "2026-11-07",
            review_overdue: overdue,
            source_url: "https://developers.openai.com/api/docs/models/gpt-6.1-sol",
            rationale: "Graph reasoning",
          }],
        }),
      },
    ]);
    renderPage();
    const status = await screen.findByLabelText("Automation readiness");
    expect(status).toHaveTextContent("Draft model: gpt-4o-mini");
    expect(status).toHaveTextContent(label);
    expect(status).toHaveTextContent("next review due 2026-11-07");
    if (recommend) expect(status).toHaveTextContent("update to gpt-6.1-sol");
    else expect(status).not.toHaveTextContent("update to gpt-6.1-sol");
  });

  it.each([
    {
      readiness: READY_RUNTIME,
      expectedWorker: "Ready",
      expectedProvider: "Connected",
      expectedMessage:
        "Automatic drafting is ready. Reviews still require a person to accept changes.",
    },
    {
      readiness: {
        background_worker_enabled: false,
        provider: "custom",
        provider_credential_configured: false,
        scheduler_enabled: false,
        source_revision: SOURCE_REVISION,
      },
      expectedWorker: "Needs operator setup",
      expectedProvider: "Needs operator setup",
      expectedMessage:
        "Your review time will be saved, but automatic drafting is not ready until the host operator completes the items above.",
    },
  ])(
    "presents scheduler and provider readiness",
    async ({
      readiness,
      expectedWorker,
      expectedProvider,
      expectedMessage,
    }) => {
      installFetchMock([
        {
          match: "/auth/setup-readiness",
          response: apiResponse(readiness),
        },
      ]);

      renderPage();

      const status = await screen.findByLabelText("Automation readiness");
      expect(status).toHaveTextContent("Scheduled background worker");
      expect(status).toHaveTextContent(expectedWorker);
      expect(status).toHaveTextContent(`Draft provider (${readiness.provider})`);
      expect(status).toHaveTextContent(expectedProvider);
      expect(status).toHaveTextContent(expectedMessage);
    }
  );

  it("creates the first project and selects it for the remaining setup", async () => {
    const refreshProjects = vi.fn().mockResolvedValue([PROJECT]);
    const setSelectedProjectId = vi.fn();
    const setBusy = vi.fn();
    const setFlash = vi.fn();
    let projectBody = null;
    const fetchMock = installFetchMock([
      {
        match: "/auth/setup-readiness",
        response: apiResponse(READY_RUNTIME),
      },
      {
        match: "/projects",
        method: "POST",
        response: (request) => {
          projectBody = JSON.parse(request.init.body);
          return apiResponse(PROJECT, 201);
        },
      },
    ]);

    renderPage({
      refreshProjects,
      setBusy,
      setFlash,
      setSelectedProjectId,
    });

    fireEvent.change(screen.getByLabelText("Project name"), {
      target: { value: "Deerhake lab" },
    });
    fireEvent.change(screen.getByLabelText("Short description"), {
      target: { value: "Track the lab's first research program." },
    });
    fireEvent.click(screen.getByRole("button", { name: "Create project" }));

    await waitFor(() => {
      expect(projectBody).toEqual({
        description: "Track the lab's first research program.",
        name: "Deerhake lab",
      });
      expect(refreshProjects).toHaveBeenCalledOnce();
      expect(setSelectedProjectId).toHaveBeenCalledWith("project-1");
    });
    expect(fetchMock).toHaveBeenCalledWith(
      "/projects",
      expect.objectContaining({
        headers: expect.objectContaining({
          Authorization: "Bearer user-token",
        }),
        method: "POST",
      })
    );
    expect(setBusy).toHaveBeenNthCalledWith(1, true);
    expect(setBusy).toHaveBeenLastCalledWith(false);
    expect(setFlash).toHaveBeenLastCalledWith(
      "Project created. Next, choose your daily review time."
    );
  });

  it("loads and saves the invitee's authenticated daily review schedule", async () => {
    let settingsBody = null;
    const fetchMock = installFetchMock([
      {
        match: "/auth/setup-readiness",
        response: apiResponse(READY_RUNTIME),
      },
      {
        match: "/projects/project-1/graph-draft-batch-settings",
        response: apiResponse({
          cadence_minutes: 1440,
          enabled: true,
          next_run_at: "2026-07-24T22:00:00Z",
          project_id: "project-1",
          run_at_local_time: "18:00",
          settings_id: "settings-1",
          timezone_name: "America/New_York",
          user_id: "user-1",
        }),
      },
      {
        match: "/projects/project-1/graph-draft-batch-settings",
        method: "PATCH",
        response: (request) => {
          settingsBody = JSON.parse(request.init.body);
          return apiResponse({
            ...settingsBody,
            next_run_at: "2026-07-25T10:30:00Z",
            project_id: "project-1",
            settings_id: "settings-1",
          });
        },
      },
    ]);

    renderPage({
      projects: [PROJECT],
      selectedProjectId: "project-1",
    });

    await waitFor(() => {
      expect(screen.getByLabelText("Cadence")).toHaveValue("1440");
      expect(screen.getByLabelText("Local run time")).toHaveValue("18:00");
      expect(screen.getByLabelText("Time zone")).toHaveValue(
        "America/New_York"
      );
    });

    fireEvent.change(screen.getByLabelText("Cadence"), {
      target: { value: "720" },
    });
    fireEvent.change(screen.getByLabelText("Local run time"), {
      target: { value: "06:30" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save cadence" }));

    await waitFor(() => {
      expect(settingsBody).toEqual({
        cadence_minutes: 720,
        email_notifications_enabled: false,
        enabled: true,
        external_context_policy: "own_notes_only",
        notification_email: null,
        run_at_local_time: "06:30",
        timezone_name: "America/New_York",
      });
      expect(screen.getByText("Saved")).toBeInTheDocument();
    });
    expect(fetchMock).toHaveBeenCalledWith(
      "/projects/project-1/graph-draft-batch-settings",
      expect.objectContaining({
        headers: expect.objectContaining({
          Authorization: "Bearer user-token",
        }),
        method: "GET",
      })
    );
    expect(fetchMock).toHaveBeenCalledWith(
      "/projects/project-1/graph-draft-batch-settings",
      expect.objectContaining({
        headers: expect.objectContaining({
          Authorization: "Bearer user-token",
        }),
        method: "PATCH",
      })
    );
  });

  it("shows the latest setup commands and navigates to each setup destination", async () => {
    const navigate = vi.fn();
    installFetchMock([
      {
        match: "/auth/setup-readiness",
        response: apiResponse(READY_RUNTIME),
      },
    ]);

    renderPage({
      navigate,
      projects: [PROJECT],
      selectedProjectId: PROJECT.project_id,
    });

    await screen.findByLabelText("Automation readiness");
    const commandText = Array.from(document.querySelectorAll(".command-block"))
      .map((node) => node.textContent)
      .join("\n");
    expect(
      Array.from(document.querySelectorAll(".command-block")).every(
        (node) => !node.textContent.includes("\n")
      )
    ).toBe(true);
    expect(commandText).toContain(
      `uv tool install --force "lab-tracker @ git+https://github.com/` +
        `SamuelBrudner/lab-tracker.git@${SOURCE_REVISION}"`
    );
    expect(commandText).toContain(`uv add "lab-tracker @ git+`);
    expect(commandText).toContain("import lab_tracker_client");
    expect(commandText).toContain(
      `uv run lt setup verify-client --expected-revision ${SOURCE_REVISION}`
    );
    // This page has only the browser session, not the token's verified access.
    expect(commandText).not.toContain("lt project bind");
    expect(commandText).not.toContain("lt hooks install");
    expect(commandText).not.toContain("lt setup init");
    fireEvent.click(screen.getByRole("button", {
      name: "Verify access and finish repository setup",
    }));
    expect(navigate).toHaveBeenCalledWith("/app/agents");
    navigate.mockClear();
    expect(commandText).toContain(
      "claude mcp add --transport stdio --scope user lab-tracker -- lt-mcp"
    );
    expect(commandText).toContain("claude mcp list");
    expect(commandText).toContain("codex mcp add lab-tracker -- lt-mcp");
    expect(commandText).toContain(
      `lt setup verify-mcp --expected-revision ${SOURCE_REVISION}`
    );
    expect(commandText).toContain("codex mcp list");
    expect(document.body.textContent).toContain(
      "The skill installer covers Claude and Codex user skill homes."
    );
    // Step 5 names each client's own route instead of a CLI-only path.
    const clientStep = screen
      .getByRole("heading", { name: "Connect your coding assistant" })
      .closest("li");
    const clientText = clientStep.textContent;
    expect(clientText).toContain("Claude Code (terminal, IDE, or the Claude Desktop Code tab)");
    expect(clientText).toContain("Claude Desktop chat");
    expect(clientText).toContain("manual registration only");
    expect(clientText).toContain("Codex in the ChatGPT desktop app");
    expect(clientText).toContain("Codex CLI (needs the codex command on your PATH)");
    expect(clientText).toContain("command not found: codex");
    const docsLink = within(clientStep).getByRole("link", { name: /per-client steps/ });
    expect(docsLink).toHaveAttribute(
      "href",
      `https://github.com/SamuelBrudner/lab-tracker/blob/${SOURCE_REVISION}/docs/agent-setup.md#choose-your-client`
    );
    // Same link as the Agents page, which must not navigate the app away.
    expect(docsLink).toHaveAttribute("target", "_blank");
    expect(docsLink).toHaveAttribute("rel", "noopener noreferrer");

    fireEvent.click(
      screen.getByRole("button", { name: "Create an agent token" })
    );
    fireEvent.click(screen.getByRole("button", { name: "Set up a device" }));
    fireEvent.click(
      screen.getByRole("button", { name: "Finish in workspace" })
    );

    expect(navigate).toHaveBeenNthCalledWith(1, "/app/agents");
    expect(navigate).toHaveBeenNthCalledWith(2, "/app/devices");
    expect(navigate).toHaveBeenNthCalledWith(3, "/app");
  });

  it("fails closed when the deployment does not report an immutable client revision", async () => {
    installFetchMock([
      {
        match: "/auth/setup-readiness",
        response: apiResponse({
          ...READY_RUNTIME,
          source_revision: "unknown",
        }),
      },
    ]);

    renderPage({
      projects: [PROJECT],
      selectedProjectId: PROJECT.project_id,
    });

    expect(
      await screen.findByText(/Matching client installation is unavailable/)
    ).toBeInTheDocument();
    const commandText = Array.from(document.querySelectorAll(".command-block"))
      .map((node) => node.textContent)
      .join("\n");
    expect(commandText).not.toContain("uv tool install");
    expect(commandText).not.toContain("codex mcp add");
    expect(commandText).not.toContain("claude mcp add");
    expect(commandText).not.toContain("lt setup init");
    expect(commandText).not.toContain("lt project bind");
    expect(commandText).not.toContain("lt hooks install");
    expectClientStepWithheld();
    expect(document.body.textContent).toContain("Do not install from GitHub main");
    expect(document.body.textContent).toContain(
      "Local repository commands are withheld"
    );
  });

  it("fails closed when setup readiness cannot be loaded", async () => {
    installFetchMock([
      {
        match: "/auth/setup-readiness",
        response: errorResponse("Readiness unavailable.", 503),
      },
    ]);

    renderPage({
      projects: [PROJECT],
      selectedProjectId: PROJECT.project_id,
    });

    expect(
      await screen.findByText(/matching client revision could not be checked/i)
    ).toHaveTextContent("Readiness unavailable.");
    expect(screen.queryByText(/Waiting for this server/)).not.toBeInTheDocument();
    const commandText = Array.from(document.querySelectorAll(".command-block"))
      .map((node) => node.textContent)
      .join("\n");
    expect(commandText).not.toContain("uv tool install");
    expect(commandText).not.toContain("lt setup init");
    expect(commandText).not.toContain("lt setup verify-mcp");
    expectClientStepWithheld();
  });
});
