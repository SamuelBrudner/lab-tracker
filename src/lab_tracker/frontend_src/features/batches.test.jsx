import * as React from "react";

import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { vi } from "vitest";

import { BatchCards, BatchReviewPage, PendingBatchBanner } from "./batches.jsx";
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
        external_context_policy: "own_notes_only",
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


describe("BatchCards", () => {
  it("shows a deferred count on batch cards when proposals were deferred", () => {
    render(
      <BatchCards
        batches={[
          {
            change_set_id: "cs-deferred",
            created_at: "2026-07-16T12:00:00Z",
            deferred_count: 2,
            operation_count: 5,
            source_note_count: 3,
            status: "ready",
            summary: "Two proposals were set aside for later.",
          },
          {
            change_set_id: "cs-plain",
            created_at: "2026-07-16T12:00:00Z",
            deferred_count: 0,
            operation_count: 1,
            source_note_count: 1,
            status: "ready",
            summary: "Nothing deferred.",
          },
        ]}
        emptyMessage="No batches"
        navigate={vi.fn()}
      />
    );

    expect(screen.getByText("2 deferred")).toBeInTheDocument();
    expect(screen.getByText("5 ops")).toBeInTheDocument();
    expect(screen.queryByText("0 deferred")).not.toBeInTheDocument();
  });
});

describe("BatchReviewPage stale capture machines", () => {
  const staleNotice =
    "lab-tracker on the machine watching `fly_walking_data` (rig-7) is behind this server.";
  const figureNotice =
    "lab-tracker in an analysis-repo environment on `rig-7` is behind this server.";
  const laptopNotice = "lab-tracker on `laptop` is behind this server.";

  function coverage(projectId, captureSources) {
    return apiResponse({
      archived_unreviewed_count: 0,
      capture_sources: captureSources,
      capture_sources_truncated: false,
      open_clarification_requests: 0,
      pending_change_sets: 0,
      project_id: projectId,
      server_release: { revision: null, version: "0.5.0" },
      unplaced_count: 0,
      unreviewed_count: 0,
    });
  }

  function coverageRoute(captureSources) {
    return { match: "/projects/project-a/coverage", response: coverage("project-a", captureSources) };
  }

  function source(installId, hostLabel, updateNotice, adapter = "lt-watch") {
    return {
      capture_host_label: hostLabel,
      capture_install_id: installId,
      evidence_adapter: adapter,
      evidence_source_provider: "local-folder",
      last_capture_at: "2026-09-27T10:00:00Z",
      note_count: 1,
      release_status: updateNotice ? "behind" : "current",
      update_recommended: Boolean(updateNotice),
      update_notice: updateNotice,
    };
  }

  function reviewPage(selectedProjectId) {
    return (
      <BatchReviewPage
        token="token-1"
        projects={[
          { name: "Project A", project_id: "project-a" },
          { name: "Project B", project_id: "project-b" },
        ]}
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

  function renderReview(selectedProjectId) {
    return render(reviewPage(selectedProjectId));
  }

  function deferred() {
    let resolve;
    const promise = new Promise((settle) => {
      resolve = settle;
    });
    return { promise, resolve };
  }

  // One macrotask: every promise continuation queued before it has run.
  const nextMacrotask = () => new Promise((resolve) => setTimeout(resolve, 0));

  // A coverage response that reports when the component has read its body,
  // so a test can wait for a positive signal instead of a fixed timer count.
  function observedCoverage(projectId, captureSources) {
    const read = deferred();
    const response = coverage(projectId, captureSources);
    return {
      read: read.promise,
      response: {
        ...response,
        json: async () => {
          const payload = await response.json();
          read.resolve();
          return payload;
        },
      },
    };
  }

  it("names each machine whose client is behind the server", async () => {
    installFetchMock([
      coverageRoute([
        source("install-a", "rig-7", staleNotice),
        source("install-b", "laptop", null),
      ]),
    ]);
    renderReview("project-a");

    expect(
      await screen.findByText("A capture client needs a lab-tracker update")
    ).toBeInTheDocument();
    expect(screen.getByText(staleNotice)).toBeInTheDocument();
    expect(screen.queryByText(/laptop/)).not.toBeInTheDocument();
  });

  it("lists every stale environment on one machine with its own notice", async () => {
    installFetchMock([
      coverageRoute([
        source("install-a", "rig-7", figureNotice, "lab-tracker-client-figure"),
        source("install-a", "rig-7", staleNotice, "lt-watch"),
      ]),
    ]);
    renderReview("project-a");

    expect(
      await screen.findByText("2 capture clients need a lab-tracker update")
    ).toBeInTheDocument();
    expect(screen.getByText(figureNotice)).toBeInTheDocument();
    expect(screen.getByText(staleNotice)).toBeInTheDocument();
  });

  it("stays silent when every machine is current", async () => {
    const observed = observedCoverage("project-a", [source("install-b", "laptop", null)]);
    installFetchMock([{ match: "/projects/project-a/coverage", response: observed.response }]);
    renderReview("project-a");

    // Inside act, the state update that follows the read is flushed before
    // act returns, whatever the scheduler's timing.
    await act(async () => {
      await observed.read;
      await nextMacrotask();
    });
    expect(screen.queryByText(/needs? a lab-tracker update/)).not.toBeInTheDocument();
    expect(screen.queryByText(/Could not check capture machines/)).not.toBeInTheDocument();
  });

  it("does not ask about machines without a selected project", async () => {
    const fetchMock = installFetchMock([]);
    renderReview("");
    await act(async () => {
      await nextMacrotask();
    });

    expect(fetchMock.mock.calls.some(([url]) => String(url).includes("/coverage"))).toBe(
      false
    );
  });

  it("drops a project's stale machines when the selected project changes", async () => {
    const lateProjectA = deferred();
    const projectAResponses = [
      lateProjectA.promise,
      coverage("project-a", [source("install-a", "rig-7", staleNotice)]),
    ];
    const projectBResponses = [
      coverage("project-b", [source("install-b", "laptop", laptopNotice)]),
      new Promise(() => {}),
    ];
    const fetchMock = installFetchMock([
      { match: "/projects/project-a/coverage", response: () => projectAResponses.shift() },
      { match: "/projects/project-b/coverage", response: () => projectBResponses.shift() },
    ]);
    const view = renderReview("project-a");

    // Project A's check is still in flight when project B is selected.
    view.rerender(reviewPage("project-b"));
    expect(await screen.findByText(laptopNotice)).toBeInTheDocument();
    await act(async () => {
      lateProjectA.resolve(coverage("project-a", [source("install-a", "rig-7", staleNotice)]));
      await nextMacrotask();
    });
    expect(screen.queryByText(staleNotice)).not.toBeInTheDocument();
    expect(screen.getByText(laptopNotice)).toBeInTheDocument();

    // A banner already shown for project A is cleared as soon as project B is
    // selected, before B's own check answers.
    view.rerender(reviewPage("project-a"));
    expect(await screen.findByText(staleNotice)).toBeInTheDocument();
    view.rerender(reviewPage("project-b"));
    expect(screen.queryByText(staleNotice)).not.toBeInTheDocument();
    expect(screen.queryByText(laptopNotice)).not.toBeInTheDocument();
    expect(
      fetchMock.mock.calls.filter(([url]) => url === "/projects/project-b/coverage")
    ).toHaveLength(2);
  });

  it("reports a failed check without hiding the queues", async () => {
    installFetchMock([
      {
        match: "/projects/project-a/coverage",
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
