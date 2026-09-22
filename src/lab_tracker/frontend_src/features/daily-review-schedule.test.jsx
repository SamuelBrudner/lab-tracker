import * as React from "react";

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { apiResponse, errorResponse, installFetchMock } from "../test/utils.js";
import { DailyReviewScheduleForm } from "./daily-review-schedule.jsx";

describe("DailyReviewScheduleForm", () => {
  it("loads and saves the authenticated user's personal settings", async () => {
    let settingsBody = null;
    const onSaved = vi.fn();
    const setBusy = vi.fn();
    const setFlash = vi.fn();
    const fetchMock = installFetchMock([
      {
        match: "/projects/project-1/graph-draft-batch-settings",
        response: apiResponse({
          cadence_minutes: 720,
          email_notifications_enabled: true,
          enabled: true,
          next_run_at: "2026-07-24T01:15:00Z",
          notification_email: "reviewer@example.edu",
          project_id: "project-1",
          review_email_available: true,
          run_at_local_time: "21:15",
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
            next_run_at: "2026-07-24T10:00:00Z",
            project_id: "project-1",
            settings_id: "settings-1",
            user_id: "user-1",
          });
        },
      },
    ]);

    render(
      <DailyReviewScheduleForm
        token="token-1"
        projectId="project-1"
        canManage={true}
        setBusy={setBusy}
        setFlash={setFlash}
        onSaved={onSaved}
      />
    );

    await waitFor(() => {
      expect(screen.getByLabelText("Cadence")).toHaveValue("720");
      expect(screen.getByLabelText("Local run time")).toHaveValue("21:15");
      expect(screen.getByLabelText("Time zone")).toHaveValue(
        "America/New_York"
      );
    });
    expect(screen.getByLabelText("Enabled")).toBeChecked();
    expect(
      screen.getByLabelText("Email me when a review is ready")
    ).toBeChecked();
    expect(screen.getByLabelText("Notification email")).toHaveValue(
      "reviewer@example.edu"
    );
    expect(screen.getByText(/^Next run:/)).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledWith(
      "/projects/project-1/graph-draft-batch-settings",
      expect.objectContaining({
        headers: expect.objectContaining({
          Authorization: "Bearer token-1",
        }),
        method: "GET",
      })
    );

    fireEvent.click(
      screen.getByRole("button", { name: "Morning (6:00 AM)" })
    );
    expect(screen.getByLabelText("Local run time")).toHaveValue("06:00");
    fireEvent.click(
      screen.getByLabelText("Email me when a review is ready")
    );
    expect(screen.getByLabelText("Notification email")).toBeDisabled();
    fireEvent.click(
      screen.getByLabelText("Email me when a review is ready")
    );
    fireEvent.click(screen.getByRole("button", { name: "Save cadence" }));

    await waitFor(() => {
      expect(settingsBody).toEqual({
        cadence_minutes: 720,
        email_notifications_enabled: true,
        enabled: true,
        notification_email: "reviewer@example.edu",
        run_at_local_time: "06:00",
        timezone_name: "America/New_York",
      });
    });
    expect(onSaved).toHaveBeenCalledWith(
      expect.objectContaining({
        run_at_local_time: "06:00",
        user_id: "user-1",
      })
    );
    expect(setBusy).toHaveBeenNthCalledWith(1, true);
    expect(setBusy).toHaveBeenLastCalledWith(false);
    expect(setFlash).toHaveBeenLastCalledWith(
      "Daily review schedule updated."
    );
  });

  it("uses the detected time zone and lets the server resolve the personal target", async () => {
    vi.spyOn(Intl, "DateTimeFormat").mockImplementation(() => ({
      resolvedOptions: () => ({ timeZone: "America/Chicago" }),
    }));

    let settingsBody = null;
    const fetchMock = installFetchMock([
      {
        match: "/projects/project-2/graph-draft-batch-settings",
        response: apiResponse({
          cadence_minutes: null,
          email_notifications_enabled: false,
          enabled: false,
          next_run_at: null,
          notification_email: null,
          project_id: "project-2",
          review_email_available: true,
          run_at_local_time: null,
          settings_id: "settings-2",
          timezone_name: null,
        }),
      },
      {
        match: "/projects/project-2/graph-draft-batch-settings",
        method: "PATCH",
        response: (request) => {
          settingsBody = JSON.parse(request.init.body);
          return apiResponse({
            ...settingsBody,
            next_run_at: null,
            project_id: "project-2",
            settings_id: "settings-2",
          });
        },
      },
    ]);

    render(
      <DailyReviewScheduleForm
        token="token-2"
        projectId="project-2"
        canManage={true}
        setBusy={vi.fn()}
        setFlash={vi.fn()}
      />
    );

    await waitFor(() => {
      expect(screen.getByLabelText("Enabled")).not.toBeChecked();
      expect(screen.getByLabelText("Cadence")).toHaveValue("1440");
      expect(screen.getByLabelText("Local run time")).toHaveValue("18:00");
      expect(screen.getByLabelText("Time zone")).toHaveValue(
        "America/Chicago"
      );
      expect(
        screen.getByLabelText("Email me when a review is ready")
      ).not.toBeChecked();
      expect(screen.getByLabelText("Notification email")).toBeDisabled();
      expect(screen.getByLabelText("Notification email")).toHaveValue("");
    });
    expect(fetchMock).toHaveBeenCalledWith(
      "/projects/project-2/graph-draft-batch-settings",
      expect.objectContaining({ method: "GET" })
    );

    fireEvent.click(screen.getByLabelText("Enabled"));
    fireEvent.click(
      screen.getByLabelText("Email me when a review is ready")
    );
    fireEvent.change(screen.getByLabelText("Notification email"), {
      target: { value: "  chicago-reviewer@example.edu  " },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save cadence" }));

    await waitFor(() => {
      expect(settingsBody).toEqual({
        cadence_minutes: 1440,
        email_notifications_enabled: true,
        enabled: true,
        notification_email: "chicago-reviewer@example.edu",
        run_at_local_time: "18:00",
        timezone_name: "America/Chicago",
      });
    });
    expect(settingsBody).not.toHaveProperty("user_id");
  });

  it("cannot save an email opt-in when host delivery is unavailable", async () => {
    let settingsBody = null;
    installFetchMock([
      {
        match: "/projects/project-3/graph-draft-batch-settings",
        response: apiResponse({
          cadence_minutes: 1440,
          email_notifications_enabled: true,
          enabled: true,
          next_run_at: null,
          notification_email: "stale@example.edu",
          project_id: "project-3",
          review_email_available: false,
          run_at_local_time: "18:00",
          settings_id: "settings-3",
          timezone_name: "America/New_York",
        }),
      },
      {
        match: "/projects/project-3/graph-draft-batch-settings",
        method: "PATCH",
        response: (request) => {
          settingsBody = JSON.parse(request.init.body);
          return apiResponse({
            ...settingsBody,
            next_run_at: null,
            project_id: "project-3",
            review_email_available: false,
            settings_id: "settings-3",
          });
        },
      },
    ]);

    render(
      <DailyReviewScheduleForm
        token="token-3"
        projectId="project-3"
        canManage={true}
        setBusy={vi.fn()}
        setFlash={vi.fn()}
      />
    );

    expect(
      await screen.findByText(/host has not configured delivery/i)
    ).toBeInTheDocument();
    expect(
      screen.queryByLabelText("Email me when a review is ready")
    ).not.toBeInTheDocument();
    expect(
      screen.queryByLabelText("Notification email")
    ).not.toBeInTheDocument();

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
  });
  it("ignores a late settings response for a previously selected project", async () => {
    const pending = {};
    function settingsFor(projectId, overrides) {
      return apiResponse({
        cadence_minutes: 1440,
        email_notifications_enabled: false,
        enabled: true,
        next_run_at: null,
        notification_email: null,
        project_id: projectId,
        review_email_available: false,
        run_at_local_time: "18:00",
        settings_id: `settings-${projectId}`,
        timezone_name: "UTC",
        user_id: "user-1",
        ...overrides,
      });
    }
    function gated(projectId) {
      return () =>
        new Promise((resolve) => {
          pending[projectId] = resolve;
        });
    }
    let patchBody = null;
    installFetchMock([
      {
        match: "/projects/project-a/graph-draft-batch-settings",
        response: gated("project-a"),
      },
      {
        match: "/projects/project-b/graph-draft-batch-settings",
        response: gated("project-b"),
      },
      {
        match: "/projects/project-b/graph-draft-batch-settings",
        method: "PATCH",
        response: (request) => {
          patchBody = JSON.parse(request.init.body);
          return settingsFor("project-b", patchBody);
        },
      },
    ]);
    const props = {
      token: "token-1",
      canManage: true,
      setBusy: vi.fn(),
      setFlash: vi.fn(),
    };

    const { rerender } = render(
      <DailyReviewScheduleForm {...props} projectId="project-a" />
    );
    await waitFor(() => expect(pending["project-a"]).toBeTypeOf("function"));
    rerender(<DailyReviewScheduleForm {...props} projectId="project-b" />);
    await waitFor(() => expect(pending["project-b"]).toBeTypeOf("function"));

    pending["project-b"](
      settingsFor("project-b", { cadence_minutes: 720, timezone_name: "UTC" })
    );
    await waitFor(() => expect(screen.getByLabelText("Cadence")).toHaveValue("720"));
    expect(screen.getByLabelText("Cadence")).toBeEnabled();

    // Project A's slower response lands last; it must not overwrite B's form.
    pending["project-a"](
      settingsFor("project-a", {
        cadence_minutes: 10080,
        run_at_local_time: "07:30",
        timezone_name: "Asia/Tokyo",
      })
    );
    await new Promise((resolve) => setTimeout(resolve, 0));
    await new Promise((resolve) => setTimeout(resolve, 0));

    expect(screen.getByLabelText("Cadence")).toHaveValue("720");
    expect(screen.getByLabelText("Time zone")).toHaveValue("UTC");
    expect(screen.getByLabelText("Local run time")).toHaveValue("18:00");

    fireEvent.click(screen.getByRole("button", { name: "Save cadence" }));
    await waitFor(() => expect(patchBody).not.toBeNull());
    expect(patchBody).toMatchObject({
      cadence_minutes: 720,
      run_at_local_time: "18:00",
      timezone_name: "UTC",
    });
  });

  it("keeps the current project's loading state when a stale request settles", async () => {
    const pending = {};
    installFetchMock([
      {
        match: "/projects/project-a/graph-draft-batch-settings",
        response: () =>
          new Promise((resolve) => {
            pending["project-a"] = resolve;
          }),
      },
      {
        match: "/projects/project-b/graph-draft-batch-settings",
        response: () =>
          new Promise((resolve) => {
            pending["project-b"] = resolve;
          }),
      },
    ]);
    const props = { token: "token-1", canManage: true, setBusy: vi.fn(), setFlash: vi.fn() };

    const { rerender } = render(<DailyReviewScheduleForm {...props} projectId="project-a" />);
    await waitFor(() => expect(pending["project-a"]).toBeTypeOf("function"));
    rerender(<DailyReviewScheduleForm {...props} projectId="project-b" />);
    await waitFor(() => expect(pending["project-b"]).toBeTypeOf("function"));

    pending["project-a"](apiResponse({ cadence_minutes: 10080, project_id: "project-a" }));
    await new Promise((resolve) => setTimeout(resolve, 0));
    await new Promise((resolve) => setTimeout(resolve, 0));

    // B is still loading, so the form must stay disabled.
    expect(screen.getByLabelText("Cadence")).toBeDisabled();
    expect(screen.getByLabelText("Cadence")).toHaveValue("1440");
  });
  it("never offers a previous project's values for saving when the next load fails", async () => {
    const fetchMock = installFetchMock([
      {
        match: "/projects/project-a/graph-draft-batch-settings",
        response: apiResponse({
          cadence_minutes: 10080,
          email_notifications_enabled: false,
          enabled: false,
          next_run_at: null,
          notification_email: null,
          project_id: "project-a",
          review_email_available: false,
          run_at_local_time: "07:00",
          timezone_name: "Asia/Tokyo",
        }),
      },
      {
        match: "/projects/project-b/graph-draft-batch-settings",
        response: errorResponse("Settings unavailable.", 500),
      },
    ]);
    const props = { token: "token-1", canManage: true, setBusy: vi.fn(), setFlash: vi.fn() };

    const { rerender } = render(<DailyReviewScheduleForm {...props} projectId="project-a" />);
    await waitFor(() => expect(screen.getByLabelText("Cadence")).toHaveValue("10080"));
    rerender(<DailyReviewScheduleForm {...props} projectId="project-b" />);

    // While B loads, A's values must not be shown under B.
    expect(screen.getByLabelText("Cadence")).toHaveValue("1440");
    expect(screen.getByLabelText("Local run time")).toHaveValue("18:00");
    expect(screen.getByLabelText("Enabled")).toBeChecked();

    await waitFor(() =>
      expect(props.setFlash).toHaveBeenCalledWith("", "Settings unavailable.")
    );
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Save cadence" })).toHaveTextContent(
        "Save cadence"
      )
    );
    expect(screen.getByLabelText("Cadence")).toHaveValue("1440");
    expect(screen.getByLabelText("Time zone")).not.toHaveValue("Asia/Tokyo");
    expect(screen.getByRole("button", { name: "Save cadence" })).toBeDisabled();
    expect(screen.getByLabelText("Cadence")).toBeDisabled();

    fireEvent.submit(screen.getByRole("button", { name: "Save cadence" }).closest("form"));
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(fetchMock.mock.calls.some(([, init]) => init?.method === "PATCH")).toBe(false);
  });

  it("does not apply a previous project's save response to the current project", async () => {
    let releasePatch = null;
    const onSaved = vi.fn();
    installFetchMock([
      {
        match: "/projects/project-a/graph-draft-batch-settings",
        response: apiResponse({
          cadence_minutes: 1440,
          enabled: true,
          next_run_at: null,
          project_id: "project-a",
          review_email_available: false,
          run_at_local_time: "18:00",
          timezone_name: "UTC",
        }),
      },
      {
        match: "/projects/project-a/graph-draft-batch-settings",
        method: "PATCH",
        response: () =>
          new Promise((resolve) => {
            releasePatch = resolve;
          }),
      },
      {
        match: "/projects/project-b/graph-draft-batch-settings",
        response: apiResponse({
          cadence_minutes: 720,
          enabled: true,
          next_run_at: null,
          project_id: "project-b",
          review_email_available: false,
          run_at_local_time: "18:00",
          timezone_name: "UTC",
        }),
      },
    ]);
    const props = {
      token: "token-1",
      canManage: true,
      setBusy: vi.fn(),
      setFlash: vi.fn(),
      onSaved,
    };

    const { rerender } = render(<DailyReviewScheduleForm {...props} projectId="project-a" />);
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Save cadence" })).toBeEnabled()
    );
    fireEvent.click(screen.getByRole("button", { name: "Save cadence" }));
    await waitFor(() => expect(releasePatch).toBeTypeOf("function"));

    rerender(<DailyReviewScheduleForm {...props} projectId="project-b" />);
    await waitFor(() => expect(screen.getByLabelText("Cadence")).toHaveValue("720"));
    expect(screen.getByText(/Email cues are unavailable/)).toBeInTheDocument();

    releasePatch(
      apiResponse({
        cadence_minutes: 1440,
        enabled: true,
        next_run_at: "2026-07-24T01:15:00Z",
        project_id: "project-a",
        review_email_available: true,
        run_at_local_time: "18:00",
        timezone_name: "UTC",
      })
    );
    await waitFor(() => expect(props.setBusy).toHaveBeenLastCalledWith(false));
    await new Promise((resolve) => setTimeout(resolve, 0));

    // The stale save must not report success into project B's context.
    expect(onSaved).not.toHaveBeenCalled();
    expect(props.setFlash).not.toHaveBeenCalledWith("Daily review schedule updated.");
    expect(screen.queryByText(/Next run:/)).not.toBeInTheDocument();
    expect(screen.getByText(/Email cues are unavailable/)).toBeInTheDocument();
    expect(screen.getByLabelText("Cadence")).toHaveValue("720");
  });
});
