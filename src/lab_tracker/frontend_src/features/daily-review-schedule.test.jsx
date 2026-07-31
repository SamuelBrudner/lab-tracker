import * as React from "react";

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { apiResponse, installFetchMock } from "../test/utils.js";
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
});
