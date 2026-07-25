import * as React from "react";

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { apiResponse, installFetchMock } from "../test/utils.js";
import {
  OnboardingPage,
  STARTER_CONTEXT_MAX_CHARS,
} from "./onboarding.jsx";

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

  it("requires explicit external-provider acknowledgement before uploading context", async () => {
    const setFlash = vi.fn();
    const fetchMock = installFetchMock([
      {
        match: "/auth/setup-readiness",
        response: apiResponse(READY_RUNTIME),
      },
      {
        match:
          "/projects/project-1/graph-draft-batch-settings?user_id=user-1",
        response: apiResponse({
          cadence_minutes: 1440,
          enabled: true,
          project_id: "project-1",
          review_email_available: false,
          run_at_local_time: "18:00",
          timezone_name: "America/New_York",
        }),
      },
    ]);

    renderPage({
      projects: [PROJECT],
      selectedProjectId: PROJECT.project_id,
      setFlash,
    });

    fireEvent.change(
      screen.getByLabelText("Grant, aims, or project context"),
      {
        target: {
          value: "Aim 1: map circuit dynamics during adaptive learning.",
        },
      }
    );
    fireEvent.click(
      screen.getByRole("button", { name: "Propose starter questions" })
    );

    await waitFor(() => {
      expect(setFlash).toHaveBeenLastCalledWith(
        "",
        expect.stringMatching(/Confirm that this context may be sent/i)
      );
    });
    expect(
      fetchMock.mock.calls.some(
        ([url, init]) =>
          String(init?.method || "GET").toUpperCase() === "POST" &&
          String(url).startsWith("/notes")
      )
    ).toBe(false);
  });

  it("blocks oversized grant context before it can be sent to the provider", async () => {
    const setFlash = vi.fn();
    const fetchMock = installFetchMock([
      {
        match: "/auth/setup-readiness",
        response: apiResponse(READY_RUNTIME),
      },
      {
        match:
          "/projects/project-1/graph-draft-batch-settings?user_id=user-1",
        response: apiResponse({
          cadence_minutes: 1440,
          enabled: true,
          project_id: "project-1",
          review_email_available: false,
          run_at_local_time: "18:00",
          timezone_name: "America/New_York",
        }),
      },
    ]);

    renderPage({
      projects: [PROJECT],
      selectedProjectId: PROJECT.project_id,
      setFlash,
    });

    fireEvent.change(
      screen.getByLabelText("Grant, aims, or project context"),
      { target: { value: "G".repeat(STARTER_CONTEXT_MAX_CHARS + 1) } }
    );
    fireEvent.click(screen.getByLabelText(/Allow this context to be sent/i));
    fireEvent.click(
      screen.getByRole("button", { name: "Propose starter questions" })
    );

    expect(setFlash).toHaveBeenLastCalledWith(
      "",
      expect.stringMatching(/maximum for a complete starter-question draft/i)
    );
    expect(
      fetchMock.mock.calls.some(
        ([url, init]) =>
          String(init?.method || "GET").toUpperCase() === "POST" &&
          String(url).startsWith("/notes")
      )
    ).toBe(false);
  });

  it("stages acknowledged context with deterministic retry keys and opens only a ready draft", async () => {
    const navigate = vi.fn();
    const setFlash = vi.fn();
    let noteBody = null;
    let draftBody = null;
    installFetchMock([
      {
        match: "/auth/setup-readiness",
        response: apiResponse(READY_RUNTIME),
      },
      {
        match:
          "/projects/project-1/graph-draft-batch-settings?user_id=user-1",
        response: apiResponse({
          cadence_minutes: 1440,
          enabled: true,
          project_id: "project-1",
          review_email_available: false,
          run_at_local_time: "18:00",
          timezone_name: "America/New_York",
        }),
      },
      {
        match: "/notes",
        method: "POST",
        response: (request) => {
          noteBody = JSON.parse(request.init.body);
          return apiResponse({ note_id: "note-1" }, 201);
        },
      },
      {
        match: "/notes/note-1/graph-drafts",
        method: "POST",
        response: (request) => {
          draftBody = JSON.parse(request.init.body);
          return apiResponse(
            { change_set_id: "draft-1", status: "ready" },
            201
          );
        },
      },
    ]);

    renderPage({
      navigate,
      projects: [PROJECT],
      selectedProjectId: PROJECT.project_id,
      setFlash,
    });

    const context =
      "Aim 1: map circuit dynamics during adaptive learning.";
    fireEvent.change(
      screen.getByLabelText("Grant, aims, or project context"),
      { target: { value: context } }
    );
    fireEvent.click(
      screen.getByLabelText(/Allow this context to be sent/i)
    );
    fireEvent.click(
      screen.getByRole("button", { name: "Propose starter questions" })
    );

    await waitFor(() => {
      expect(noteBody).toEqual({
        client_capture_id: expect.stringMatching(
          /^onboarding-context-[0-9a-f]{64}$/
        ),
        metadata: {
          context_type: "onboarding_grant_or_project_brief",
          source: "guided_setup",
        },
        project_id: "project-1",
        raw_content: `Onboarding research context\n\n${context}`,
        status: "staged",
      });
      expect(draftBody).toEqual({
        external_provider_acknowledged: true,
        idempotency_key: expect.stringMatching(
          /^starter-questions:[0-9a-f]{64}$/
        ),
        mode: "graph_context",
        purpose: "starter_questions",
        user_hint:
          "Use this onboarding context to propose a small, reviewable starting set of research questions. Preserve uncertainty, do not invent grant details, and do not create committed records.",
      });
    });
    expect(
      noteBody.client_capture_id.replace("onboarding-context-", "")
    ).toBe(draftBody.idempotency_key.replace("starter-questions:", ""));
    expect(noteBody.client_capture_id).not.toContain(context);
    expect(draftBody.idempotency_key).not.toContain(context);
    expect(navigate).toHaveBeenCalledWith("/app/graph-drafts/draft-1");
    expect(setFlash).toHaveBeenLastCalledWith(
      "Starter questions are ready for review."
    );
    expect(
      screen.getByLabelText("Grant, aims, or project context")
    ).toHaveValue("");
  });

  it("does not present a truncated starter draft as fully ready", async () => {
    const navigate = vi.fn();
    const setFlash = vi.fn();
    installFetchMock([
      {
        match: "/auth/setup-readiness",
        response: apiResponse(READY_RUNTIME),
      },
      {
        match:
          "/projects/project-1/graph-draft-batch-settings?user_id=user-1",
        response: apiResponse({
          cadence_minutes: 1440,
          enabled: true,
          project_id: "project-1",
          review_email_available: false,
          run_at_local_time: "18:00",
          timezone_name: "America/New_York",
        }),
      },
      {
        match: "/notes",
        method: "POST",
        response: apiResponse({ note_id: "note-truncated" }, 201),
      },
      {
        match: "/notes/note-truncated/graph-drafts",
        method: "POST",
        response: apiResponse(
          {
            change_set_id: "draft-truncated",
            source_context_truncated: true,
            status: "ready",
          },
          201
        ),
      },
    ]);

    renderPage({
      navigate,
      projects: [PROJECT],
      selectedProjectId: PROJECT.project_id,
      setFlash,
    });

    const context = "Specific Aim 1: characterize the complete mechanism.";
    fireEvent.change(
      screen.getByLabelText("Grant, aims, or project context"),
      { target: { value: context } }
    );
    fireEvent.click(screen.getByLabelText(/Allow this context to be sent/i));
    fireEvent.click(
      screen.getByRole("button", { name: "Propose starter questions" })
    );

    await waitFor(() => {
      expect(setFlash).toHaveBeenLastCalledWith(
        "",
        expect.stringMatching(/provider received only part/i)
      );
    });
    expect(navigate).not.toHaveBeenCalled();
    expect(
      screen.getByLabelText("Grant, aims, or project context")
    ).toHaveValue(context);
  });

  it("reports a queued starter draft truthfully and retains the source context", async () => {
    const navigate = vi.fn();
    const setFlash = vi.fn();
    installFetchMock([
      {
        match: "/auth/setup-readiness",
        response: apiResponse(READY_RUNTIME),
      },
      {
        match:
          "/projects/project-1/graph-draft-batch-settings?user_id=user-1",
        response: apiResponse({
          cadence_minutes: 1440,
          enabled: true,
          project_id: "project-1",
          review_email_available: false,
          run_at_local_time: "18:00",
          timezone_name: "America/New_York",
        }),
      },
      {
        match: "/notes",
        method: "POST",
        response: apiResponse({ note_id: "note-queued" }, 201),
      },
      {
        match: "/notes/note-queued/graph-drafts",
        method: "POST",
        response: apiResponse(
          { change_set_id: "draft-queued", status: "drafting" },
          201
        ),
      },
    ]);

    renderPage({
      navigate,
      projects: [PROJECT],
      selectedProjectId: PROJECT.project_id,
      setFlash,
    });

    const context = "A project brief that should remain while queued.";
    fireEvent.change(
      screen.getByLabelText("Grant, aims, or project context"),
      { target: { value: context } }
    );
    fireEvent.click(
      screen.getByLabelText(/Allow this context to be sent/i)
    );
    fireEvent.click(
      screen.getByRole("button", { name: "Propose starter questions" })
    );

    await waitFor(() => {
      expect(setFlash).toHaveBeenLastCalledWith(
        "Research context saved. Starter questions are queued and still being prepared."
      );
    });
    expect(setFlash).not.toHaveBeenCalledWith(
      "Starter questions are ready for review."
    );
    expect(
      screen.getByLabelText("Grant, aims, or project context")
    ).toHaveValue(context);
    expect(navigate).not.toHaveBeenCalled();
  });

  it("reports starter-draft failure without discarding retryable context", async () => {
    const navigate = vi.fn();
    const setFlash = vi.fn();
    installFetchMock([
      {
        match: "/auth/setup-readiness",
        response: apiResponse(READY_RUNTIME),
      },
      {
        match:
          "/projects/project-1/graph-draft-batch-settings?user_id=user-1",
        response: apiResponse({
          cadence_minutes: 1440,
          enabled: true,
          project_id: "project-1",
          review_email_available: false,
          run_at_local_time: "18:00",
          timezone_name: "America/New_York",
        }),
      },
      {
        match: "/notes",
        method: "POST",
        response: apiResponse({ note_id: "note-failed" }, 201),
      },
      {
        match: "/notes/note-failed/graph-drafts",
        method: "POST",
        response: apiResponse(
          { change_set_id: "draft-failed", status: "failed" },
          201
        ),
      },
    ]);

    renderPage({
      navigate,
      projects: [PROJECT],
      selectedProjectId: PROJECT.project_id,
      setFlash,
    });

    const context = "A grant abstract that must survive a failed draft.";
    fireEvent.change(
      screen.getByLabelText("Grant, aims, or project context"),
      { target: { value: context } }
    );
    fireEvent.click(
      screen.getByLabelText(/Allow this context to be sent/i)
    );
    fireEvent.click(
      screen.getByRole("button", { name: "Propose starter questions" })
    );

    await waitFor(() => {
      expect(setFlash).toHaveBeenLastCalledWith(
        "",
        expect.stringMatching(/starter-question drafting failed/i)
      );
    });
    expect(
      screen.getByLabelText("Grant, aims, or project context")
    ).toHaveValue(context);
    expect(navigate).not.toHaveBeenCalled();
  });

  it("reuses the same note and draft idempotency keys when retrying unchanged context", async () => {
    const navigate = vi.fn();
    const setFlash = vi.fn();
    const noteBodies = [];
    const draftBodies = [];
    installFetchMock([
      {
        match: "/auth/setup-readiness",
        response: apiResponse(READY_RUNTIME),
      },
      {
        match:
          "/projects/project-1/graph-draft-batch-settings?user_id=user-1",
        response: apiResponse({
          cadence_minutes: 1440,
          enabled: true,
          project_id: "project-1",
          review_email_available: false,
          run_at_local_time: "18:00",
          timezone_name: "America/New_York",
        }),
      },
      {
        match: "/notes",
        method: "POST",
        response: (request) => {
          noteBodies.push(JSON.parse(request.init.body));
          return apiResponse({ note_id: "note-retry" }, 201);
        },
      },
      {
        match: "/notes/note-retry/graph-drafts",
        method: "POST",
        response: (request) => {
          draftBodies.push(JSON.parse(request.init.body));
          const attempt = draftBodies.length;
          return apiResponse(
            {
              change_set_id: "draft-retry",
              status: attempt === 1 ? "failed" : "ready",
            },
            201
          );
        },
      },
    ]);

    renderPage({
      navigate,
      projects: [PROJECT],
      selectedProjectId: PROJECT.project_id,
      setFlash,
    });

    const context = "The exact same context is safe to retry.";
    fireEvent.change(
      screen.getByLabelText("Grant, aims, or project context"),
      { target: { value: context } }
    );
    fireEvent.click(
      screen.getByLabelText(/Allow this context to be sent/i)
    );
    fireEvent.click(
      screen.getByRole("button", { name: "Propose starter questions" })
    );

    await waitFor(() => {
      expect(setFlash).toHaveBeenLastCalledWith(
        "",
        expect.stringMatching(/starter-question drafting failed/i)
      );
    });
    fireEvent.click(
      screen.getByRole("button", { name: "Propose starter questions" })
    );

    await waitFor(() => {
      expect(navigate).toHaveBeenCalledWith(
        "/app/graph-drafts/draft-retry"
      );
    });
    expect(noteBodies).toHaveLength(2);
    expect(draftBodies).toHaveLength(2);
    expect(noteBodies[0].client_capture_id).toBe(
      noteBodies[1].client_capture_id
    );
    expect(draftBodies[0].idempotency_key).toBe(
      draftBodies[1].idempotency_key
    );
  });

  it("loads and saves the invitee's per-user daily review schedule", async () => {
    let settingsBody = null;
    const fetchMock = installFetchMock([
      {
        match: "/auth/setup-readiness",
        response: apiResponse(READY_RUNTIME),
      },
      {
        match:
          "/projects/project-1/graph-draft-batch-settings?user_id=user-1",
        response: apiResponse({
          cadence_minutes: 1440,
          enabled: true,
          next_run_at: "2026-07-24T22:00:00Z",
          project_id: "project-1",
          review_email_available: false,
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
        user_id: "user-1",
      });
      expect(screen.getByText("Saved")).toBeInTheDocument();
    });
    expect(fetchMock).toHaveBeenCalledWith(
      "/projects/project-1/graph-draft-batch-settings?user_id=user-1",
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
    expect(commandText).toContain(
      "lt setup init --install-skills --dry-run"
    );
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
    expect(commandText).not.toContain("lt setup init");
    expect(commandText).not.toContain("lt project bind");
    expect(commandText).not.toContain("lt hooks install");
    expect(document.body.textContent).toContain("Do not install from GitHub main");
    expect(document.body.textContent).toContain(
      "Local repository commands are withheld"
    );
  });
});
