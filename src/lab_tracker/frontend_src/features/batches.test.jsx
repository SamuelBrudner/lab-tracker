import * as React from "react";

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { vi } from "vitest";

import { BatchReviewPage, PendingBatchBanner } from "./batches.jsx";
import { apiResponse, errorResponse, installFetchMock } from "../test/utils.js";

describe("PendingBatchBanner", () => {
  it("nudges the user to flesh out a meeting when a pending batch has meeting notes", async () => {
    installFetchMock([
      {
        match: "/batches?limit=5&mine=true",
        response: apiResponse([
          {
            change_set_id: "cs-meeting",
            status: "ready",
            source_note_count: 2,
            meeting_note_count: 1,
          },
        ]),
      },
    ]);

    render(<PendingBatchBanner token="token-1" navigate={vi.fn()} />);

    expect(
      await screen.findByText(/a meeting is waiting to be fleshed out/i)
    ).toBeInTheDocument();
  });

  it("shows the plain batch count when no meeting notes are present", async () => {
    installFetchMock([
      {
        match: "/batches?limit=5&mine=true",
        response: apiResponse([
          {
            change_set_id: "cs-1",
            status: "ready",
            source_note_count: 1,
            meeting_note_count: 0,
          },
        ]),
      },
    ]);

    render(<PendingBatchBanner token="token-1" navigate={vi.fn()} />);

    expect(await screen.findByText("1 daily review ready")).toBeInTheDocument();
  });

  it("deep-links Review to the meeting batch even when it is not first", async () => {
    const navigate = vi.fn();
    installFetchMock([
      {
        match: "/batches?limit=5&mine=true",
        response: apiResponse([
          {
            change_set_id: "cs-plain",
            status: "ready",
            source_note_count: 1,
            meeting_note_count: 0,
          },
          {
            change_set_id: "cs-meeting",
            status: "ready",
            source_note_count: 1,
            meeting_note_count: 2,
          },
        ]),
      },
    ]);

    render(<PendingBatchBanner token="token-1" navigate={navigate} />);

    fireEvent.click(await screen.findByRole("button", { name: "Review" }));
    expect(navigate).toHaveBeenCalledWith("/app/batches/cs-meeting");
  });
  it("reports a failed pending-batch lookup instead of hiding the banner", async () => {
    installFetchMock([
      {
        match: "/batches?limit=5&mine=true",
        response: errorResponse("Batch service unavailable", 503),
      },
    ]);

    render(<PendingBatchBanner token="token-1" navigate={vi.fn()} />);

    expect(
      await screen.findByText(
        "Could not load your daily reviews: Batch service unavailable"
      )
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Review" })).not.toBeInTheDocument();
  });
});

describe("BatchReviewPage", () => {
  function installGatedProjectQueues(extraRoutes = []) {
    const resolvers = { "project-a": [], "project-b": [] };
    installFetchMock([
      ...extraRoutes,
      {
        match: /^\/batches(\/runs)?\?project_id=project-(a|b)&/,
        response: (request) => {
          const projectId = request.url.match(/project_id=(project-[ab])/)[1];
          const isReadyQueue =
            request.url === `/batches?project_id=${projectId}&mine=true&limit=100`;
          return new Promise((resolve) => {
            resolvers[projectId].push(() =>
              resolve(
                apiResponse(
                  isReadyQueue
                    ? [
                        {
                          change_set_id: `ready-${projectId}`,
                          created_at: "2026-07-16T12:00:00Z",
                          operation_count: 0,
                          source_note_count: 1,
                          status: "ready",
                          summary: `Ready in ${projectId}`,
                        },
                      ]
                    : []
                )
              )
            );
          });
        },
      },
      {
        match: /^\/projects\/project-(a|b)\/graph-draft-batch-settings$/,
        response: () => apiResponse({ cadence_minutes: 1440, enabled: true }),
      },
    ]);
    const settle = async (projectId) => {
      await waitFor(() => expect(resolvers[projectId]).toHaveLength(4));
      resolvers[projectId].forEach((release) => release());
    };
    return { resolvers, settle };
  }

  function renderPage(selectedProjectId) {
    const props = {
      token: "token-1",
      projects: [
        { name: "Project A", project_id: "project-a" },
        { name: "Project B", project_id: "project-b" },
      ],
      onSelectedProjectChange: vi.fn(),
      navigate: vi.fn(),
      canManageGraph: true,
      canManageProject: false,
      setBusy: vi.fn(),
      setFlash: vi.fn(),
    };
    const view = render(<BatchReviewPage {...props} selectedProjectId={selectedProjectId} />);
    return {
      ...view,
      props,
      select: (projectId) =>
        view.rerender(<BatchReviewPage {...props} selectedProjectId={projectId} />),
    };
  }

  const flushResponses = async () => {
    await new Promise((resolve) => setTimeout(resolve, 0));
    await new Promise((resolve) => setTimeout(resolve, 0));
  };

  it("ignores late queue responses for a previously selected project", async () => {
    const { settle } = installGatedProjectQueues();
    const page = renderPage("project-a");
    page.select("project-b");

    await settle("project-b");
    expect(await screen.findByText("Ready in project-b")).toBeInTheDocument();

    await settle("project-a");
    await flushResponses();

    expect(screen.getByText("Ready in project-b")).toBeInTheDocument();
    expect(screen.queryByText("Ready in project-a")).not.toBeInTheDocument();
  });

  it("keeps the current project's loading state when a stale load settles", async () => {
    const { settle } = installGatedProjectQueues();
    const page = renderPage("project-a");
    expect(screen.getByText("Loading...")).toBeInTheDocument();
    page.select("project-b");

    await settle("project-a");
    await flushResponses();

    expect(screen.getByText("Loading...")).toBeInTheDocument();
    expect(screen.queryByText("Ready in project-a")).not.toBeInTheDocument();

    await settle("project-b");
    expect(await screen.findByText("Ready in project-b")).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByText("Loading...")).not.toBeInTheDocument());
  });

  it("reloads the queues and opens the drafted batch after run now", async () => {
    const { resolvers, settle } = installGatedProjectQueues([
      {
        match: "/batches/run-now",
        method: "POST",
        response: (request) => {
          expect(JSON.parse(request.init.body)).toEqual({ project_id: "project-a" });
          return apiResponse({ change_set_id: "cs-from-a", summary: "Drafted A" });
        },
      },
    ]);
    const page = renderPage("project-a");
    await settle("project-a");
    expect(await screen.findByText("Ready in project-a")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole("button", { name: "Run now" })).toBeEnabled());

    fireEvent.click(screen.getByRole("button", { name: "Run now" }));
    await waitFor(() => expect(resolvers["project-a"]).toHaveLength(8));
    resolvers["project-a"].slice(4).forEach((release) => release());

    await waitFor(() =>
      expect(page.props.navigate).toHaveBeenCalledWith("/app/batches/cs-from-a")
    );
    expect(page.props.setBusy).toHaveBeenLastCalledWith(false);
  });

  it("does not reload or report a run-now result after the user switches projects", async () => {
    let releaseRun = null;
    const { resolvers, settle } = installGatedProjectQueues([
      {
        match: "/batches/run-now",
        method: "POST",
        response: () =>
          new Promise((resolve) => {
            releaseRun = (body) => resolve(apiResponse(body));
          }),
      },
    ]);
    const page = renderPage("project-a");
    await settle("project-a");
    expect(await screen.findByText("Ready in project-a")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole("button", { name: "Run now" })).toBeEnabled());

    fireEvent.click(screen.getByRole("button", { name: "Run now" }));
    await waitFor(() => expect(releaseRun).toBeTypeOf("function"));
    page.select("project-b");
    await settle("project-b");
    expect(await screen.findByText("Ready in project-b")).toBeInTheDocument();

    releaseRun({ change_set_id: "cs-from-a", summary: "Drafted A" });
    await flushResponses();
    await flushResponses();

    // Project A's run must not reload A's queues over B's, nor navigate away.
    expect(resolvers["project-a"]).toHaveLength(4);
    expect(screen.getByText("Ready in project-b")).toBeInTheDocument();
    expect(screen.queryByText("Ready in project-a")).not.toBeInTheDocument();
    expect(page.props.navigate).not.toHaveBeenCalled();
    expect(page.props.setFlash).toHaveBeenLastCalledWith(
      "Daily review run for Project A finished. Select it to see the results."
    );
    expect(page.props.setBusy).toHaveBeenLastCalledWith(false);
  });

  it("shows distinct personal, waiting, and owner-commit queues", async () => {
    installFetchMock([
      {
        match: "/batches?project_id=project-1&mine=true&limit=100",
        response: apiResponse([
          {
            change_set_id: "ready-1",
            created_at: "2026-07-16T12:00:00Z",
            operations: [],
            source_note_count: 1,
            status: "ready",
            summary: "Ready for this reviewer",
          },
        ]),
      },
      {
        match: "/batches?project_id=project-1&mine=true&status=submitted&limit=100",
        response: apiResponse([
          {
            change_set_id: "waiting-1",
            created_at: "2026-07-16T12:00:00Z",
            operations: [],
            source_note_count: 1,
            status: "submitted",
            summary: "Waiting for project review",
          },
          {
            change_set_id: "commit-1",
            created_at: "2026-07-16T12:00:00Z",
            operations: [],
            source_note_count: 1,
            status: "submitted",
            summary: "Needs owner commit",
          },
        ]),
      },
      {
        match: "/batches?project_id=project-1&needs_commit=true&limit=100",
        response: apiResponse([
          {
            change_set_id: "commit-1",
            created_at: "2026-07-16T12:00:00Z",
            operations: [],
            source_note_count: 1,
            status: "submitted",
            summary: "Needs owner commit",
          },
        ]),
      },
      {
        match:
          "/batches?project_id=project-1&unassigned_oversight=true&limit=100",
        response: apiResponse([
          {
            change_set_id: "legacy-1",
            created_at: "2026-07-16T12:00:00Z",
            operations: [],
            source_note_count: 1,
            status: "changes_requested",
            summary: "Legacy unassigned review",
          },
        ]),
      },
      {
        match: "/batches/runs?project_id=project-1&mine=true&limit=20",
        response: apiResponse([]),
      },
      {
        match: "/projects/project-1/graph-draft-batch-settings",
        response: apiResponse({
          cadence_minutes: 1440,
          email_notifications_enabled: false,
          enabled: false,
          next_run_at: null,
          notification_email: null,
          project_id: "project-1",
          run_at_local_time: "18:00",
          settings_id: "settings-1",
          timezone_name: "America/New_York",
          user_id: "reviewer-1",
        }),
      },
    ]);

    render(
      <BatchReviewPage
        token="token-1"
        projects={[{ name: "Project One", project_id: "project-1" }]}
        selectedProjectId="project-1"
        onSelectedProjectChange={vi.fn()}
        navigate={vi.fn()}
        canManageGraph={true}
        canManageProject={true}
        setBusy={vi.fn()}
        setFlash={vi.fn()}
      />
    );

    expect(await screen.findByText("Ready for this reviewer")).toBeInTheDocument();
    expect(screen.getByText("Waiting for project review")).toBeInTheDocument();
    expect(screen.getAllByText("Needs owner commit")).toHaveLength(1);
    expect(screen.getByText("Legacy unassigned review")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Ready for you" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Waiting on others" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Needs commit" })).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { name: "Unassigned project oversight" })
    ).toBeInTheDocument();
  });

  it("saves cadence as the reviewer's personal run-due settings", async () => {
    let settingsBody = null;
    const fetchMock = installFetchMock([
      {
        match: "/batches?project_id=project-1&mine=true&limit=100",
        response: apiResponse([], 200, { limit: 100, offset: 0, total: 0 }),
      },
      {
        match: "/batches?project_id=project-1&mine=true&status=submitted&limit=100",
        response: apiResponse([], 200, { limit: 100, offset: 0, total: 0 }),
      },
      {
        match: "/batches?project_id=project-1&needs_commit=true&limit=100",
        response: apiResponse([], 200, { limit: 100, offset: 0, total: 0 }),
      },
      {
        match:
          "/batches?project_id=project-1&unassigned_oversight=true&limit=100",
        response: apiResponse([], 200, { limit: 100, offset: 0, total: 0 }),
      },
      {
        match: "/batches/runs?project_id=project-1&mine=true&limit=20",
        response: apiResponse([], 200, { limit: 20, offset: 0, total: 0 }),
      },
      {
        match: "/projects/project-1/graph-draft-batch-settings",
        response: [
          apiResponse({
            cadence_minutes: 720,
            enabled: false,
            next_run_at: null,
            project_id: "project-1",
            run_at_local_time: "06:00",
            settings_id: "settings-1",
            timezone_name: "America/New_York",
          }),
          apiResponse({
            cadence_minutes: 1440,
            enabled: true,
            next_run_at: "2026-06-25T22:00:00Z",
            project_id: "project-1",
            run_at_local_time: "18:00",
            settings_id: "settings-1",
            timezone_name: "America/New_York",
          }),
        ],
      },
      {
        match: "/projects/project-1/graph-draft-batch-settings",
        method: "PATCH",
        response: (request) => {
          settingsBody = JSON.parse(request.init.body);
          return apiResponse({
            ...settingsBody,
            next_run_at: "2026-06-25T22:00:00Z",
            project_id: "project-1",
            settings_id: "settings-1",
          });
        },
      },
    ]);

    render(
      <BatchReviewPage
        token="token-1"
        projects={[{ name: "Project One", project_id: "project-1" }]}
        selectedProjectId="project-1"
        onSelectedProjectChange={vi.fn()}
        navigate={vi.fn()}
        canManageGraph={true}
        canManageProject={true}
        setBusy={vi.fn()}
        setFlash={vi.fn()}
      />
    );

    expect(await screen.findByRole("heading", { name: "Your cadence" })).toBeInTheDocument();
    // Wait for the settings GET to populate the form before interacting. The
    // "Enabled" checkbox defaults to checked but loads unchecked, so clicking
    // before the async load resolves lets the load clobber the toggle (the
    // request would then carry enabled: false and the save assertion flakes).
    const enabledCheckbox = screen.getByLabelText("Enabled");
    await waitFor(() => expect(enabledCheckbox).not.toBeChecked());
    fireEvent.click(enabledCheckbox);
    fireEvent.change(screen.getByLabelText("Cadence"), { target: { value: "1440" } });
    fireEvent.change(screen.getByLabelText("Local run time"), { target: { value: "18:00" } });
    fireEvent.click(screen.getByRole("button", { name: "Save cadence" }));

    await waitFor(() => {
      expect(settingsBody).toEqual({
        cadence_minutes: 1440,
        email_notifications_enabled: false,
        enabled: true,
        notification_email: null,
        run_at_local_time: "18:00",
        timezone_name: "America/New_York",
      });
    });
    expect(fetchMock).toHaveBeenCalled();
  });
});

describe("BatchReviewPage drafts from captures", () => {
  it("lists single-capture drafts beside the batch queues and opens them with a way back", async () => {
    const navigate = vi.fn();
    installFetchMock([
      { match: /^\/batches/, response: apiResponse([]) },
      {
        match: /graph-draft-batch-settings$/,
        response: apiResponse({ cadence_minutes: 1440, enabled: true }),
      },
      {
        match: "/graph-drafts?project_id=project-1&status=ready&limit=50",
        response: apiResponse([
          {
            change_set_id: "note-draft-1",
            created_at: "2026-07-16T12:00:00Z",
            draft_mode: "graph_context",
            status: "ready",
            summary: "Whiteboard photo suggests a control question",
          },
          {
            change_set_id: "batch-1",
            created_at: "2026-07-16T13:00:00Z",
            draft_mode: "graph_batch",
            status: "ready",
            summary: "A batch that belongs in the queues above",
          },
        ]),
      },
      {
        match: "/graph-drafts?project_id=project-1&status=changes_requested&limit=50",
        response: apiResponse([
          {
            change_set_id: "onboarding-1",
            created_at: "2026-07-16T14:00:00Z",
            draft_mode: "graph_context",
            purpose: "member_checkpoint_alignment",
            status: "changes_requested",
            summary: "Onboarding alignment, reviewed elsewhere",
          },
        ]),
      },
    ]);

    render(
      <BatchReviewPage
        token="token-1"
        projects={[{ name: "Project One", project_id: "project-1" }]}
        selectedProjectId="project-1"
        onSelectedProjectChange={vi.fn()}
        navigate={navigate}
        canManageGraph={true}
        canManageProject={false}
        setBusy={vi.fn()}
        setFlash={vi.fn()}
      />
    );

    expect(await screen.findByText("Whiteboard photo suggests a control question")).toBeInTheDocument();
    expect(screen.queryByText("A batch that belongs in the queues above")).not.toBeInTheDocument();
    expect(screen.queryByText("Onboarding alignment, reviewed elsewhere")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Review draft" }));
    expect(navigate).toHaveBeenCalledWith("/app/graph-drafts/note-draft-1?return_to=%2Fapp%2Fbatches");
  });
});

describe("BatchReviewPage stale capture machines", () => {
  const staleNotice =
    "lab-tracker on the machine watching `fly_walking_data` (rig-7) is behind this server.";

  function captureInstallsRoute(installs) {
    return {
      match: "/projects/project-a/capture-installs",
      response: apiResponse({
        installs,
        project_id: "project-a",
        server: { revision: null, version: "0.5.0" },
        window_days: 90,
      }),
    };
  }

  function renderReview(selectedProjectId) {
    return render(
      <BatchReviewPage
        token="token-1"
        projects={[{ name: "Project A", project_id: "project-a" }]}
        selectedProjectId={selectedProjectId}
        onSelectedProjectChange={vi.fn()}
        navigate={vi.fn()}
        canManageGraph={true}
        canManageProject={false}
        setBusy={vi.fn()}
        setFlash={vi.fn()}
      />
    );
  }

  const flushResponses = async () => {
    await new Promise((resolve) => setTimeout(resolve, 0));
    await new Promise((resolve) => setTimeout(resolve, 0));
  };

  it("names each machine whose client is behind the server", async () => {
    installFetchMock([
      captureInstallsRoute([
        { host_label: "rig-7", install_id: "install-a", notice: staleNotice },
        { host_label: "laptop", install_id: "install-b", notice: null },
      ]),
    ]);
    renderReview("project-a");

    expect(
      await screen.findByText("A capture machine needs a lab-tracker update")
    ).toBeInTheDocument();
    expect(screen.getByText(staleNotice)).toBeInTheDocument();
    expect(screen.queryByText(/laptop/)).not.toBeInTheDocument();
  });

  it("stays silent when every machine is current", async () => {
    const fetchMock = installFetchMock([
      captureInstallsRoute([{ host_label: "laptop", install_id: "install-b", notice: null }]),
    ]);
    renderReview("project-a");

    await waitFor(() =>
      expect(fetchMock.mock.calls.map(([url]) => url)).toContain(
        "/projects/project-a/capture-installs"
      )
    );
    await flushResponses();
    expect(screen.queryByText(/needs? a lab-tracker update/)).not.toBeInTheDocument();
    expect(screen.queryByText(/Could not check capture machines/)).not.toBeInTheDocument();
  });

  it("does not ask about machines without a selected project", async () => {
    const fetchMock = installFetchMock([]);
    renderReview("");
    await flushResponses();

    expect(
      fetchMock.mock.calls.some(([url]) => String(url).includes("/capture-installs"))
    ).toBe(false);
  });

  it("reports a failed check without hiding the queues", async () => {
    installFetchMock([
      {
        match: "/projects/project-a/capture-installs",
        response: errorResponse("boom", 500),
      },
    ]);
    renderReview("project-a");

    expect(
      await screen.findByText(/Could not check capture machines for updates/)
    ).toBeInTheDocument();
    expect(screen.getByText("Ready for you")).toBeInTheDocument();
  });
});
