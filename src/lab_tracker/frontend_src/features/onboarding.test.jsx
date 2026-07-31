import * as React from "react";

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { apiResponse, installFetchMock } from "../test/utils.js";
import { OnboardingPage } from "./onboarding.jsx";

const USER = {
  role: "admin",
  user_id: "user-1",
  username: "marion.deerhake@yale.edu",
};

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
  source_revision: "0123456789abcdef0123456789abcdef01234567",
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

describe("OnboardingPage", () => {
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
        provider: "agentic",
        provider_credential_configured: false,
        scheduler_enabled: false,
        source_revision: "0123456789abcdef0123456789abcdef01234567",
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

    renderPage({ navigate });

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
      "uv tool install --upgrade git+https://github.com/SamuelBrudner/lab-tracker"
    );
    expect(commandText).toContain(
      "lt setup init --install-skills --dry-run"
    );
    expect(commandText).toContain("lt setup init --install-skills --yes");
    expect(commandText).toContain(
      'lt project bind --name "YOUR PROJECT NAME" --dry-run'
    );
    expect(commandText).toContain("lt setup status");
    expect(commandText).toContain("codex mcp add lab-tracker -- lt-mcp");
    expect(commandText).toContain("codex mcp list");
    expect(document.body.textContent).toContain(
      "The skill installer covers Claude and Codex user skill homes."
    );

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
});
