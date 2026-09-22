import * as React from "react";

import { apiRequest } from "../shared/api.js";
import { formatDate } from "../shared/formatters.js";

const { useCallback, useEffect, useRef, useState } = React;

const BATCH_CADENCE_OPTIONS = [
  { label: "Daily", value: "1440" },
  { label: "Every 12 hours", value: "720" },
  { label: "Weekly", value: "10080" },
];

function detectedTimeZone() {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  } catch {
    return "UTC";
  }
}

function DailyReviewScheduleForm({
  token,
  projectId,
  canManage,
  setBusy,
  setFlash,
  onSaved = () => {},
  onRunNow = null,
}) {
  const [settings, setSettings] = useState(null);
  const [loading, setLoading] = useState(false);
  const [enabled, setEnabled] = useState(true);
  const [cadenceMinutes, setCadenceMinutes] = useState("1440");
  const [runAtLocalTime, setRunAtLocalTime] = useState("18:00");
  const [timezoneName, setTimezoneName] = useState(() => detectedTimeZone());
  const [emailNotificationsEnabled, setEmailNotificationsEnabled] =
    useState(false);
  const [notificationEmail, setNotificationEmail] = useState("");
  // Each load bumps the generation; a response (or failure) from an older
  // load — e.g. for a previously selected project — is ignored so it can never
  // populate the form that "Save cadence" PATCHes into the current project.
  const loadGenerationRef = useRef(0);
  // The project the form currently belongs to (null once unmounted), so a save
  // response for a previously selected project is not shown under this one.
  const currentProjectIdRef = useRef(projectId);

  useEffect(() => {
    currentProjectIdRef.current = projectId;
    return () => {
      currentProjectIdRef.current = null;
    };
  }, [projectId]);

  const loadSettings = useCallback(async () => {
    const generation = ++loadGenerationRef.current;
    const isCurrent = () => generation === loadGenerationRef.current;
    // Until this project's settings load, the form must neither show nor be
    // able to save values loaded (or edited) for a previous project.
    setSettings(null);
    setEnabled(true);
    setCadenceMinutes("1440");
    setRunAtLocalTime("18:00");
    setTimezoneName(detectedTimeZone());
    setEmailNotificationsEnabled(false);
    setNotificationEmail("");
    if (!projectId) {
      setLoading(false);
      return;
    }
    setLoading(true);
    try {
      const nextSettings = await apiRequest(
        `/projects/${projectId}/graph-draft-batch-settings`,
        { token }
      );
      if (!isCurrent()) {
        return;
      }
      setSettings(nextSettings);
      setEnabled(Boolean(nextSettings.enabled));
      setCadenceMinutes(String(nextSettings.cadence_minutes || 1440));
      setRunAtLocalTime(nextSettings.run_at_local_time || "18:00");
      setTimezoneName(nextSettings.timezone_name || detectedTimeZone());
      const reviewEmailAvailable =
        nextSettings.review_email_available === true;
      setEmailNotificationsEnabled(
        reviewEmailAvailable &&
          Boolean(nextSettings.email_notifications_enabled)
      );
      setNotificationEmail(
        reviewEmailAvailable ? nextSettings.notification_email || "" : ""
      );
    } catch (err) {
      if (!isCurrent()) {
        return;
      }
      setFlash("", err.message || "Failed to load daily review timing.");
    } finally {
      if (isCurrent()) {
        setLoading(false);
      }
    }
  }, [projectId, setFlash, token]);

  useEffect(() => {
    loadSettings();
    return () => {
      // Invalidate the in-flight load on project change or unmount.
      loadGenerationRef.current += 1;
    };
  }, [loadSettings]);

  async function saveSettings(event) {
    event.preventDefault();
    if (!projectId || !canManage || !settings) {
      return;
    }
    const savedProjectId = projectId;
    setBusy(true);
    setFlash("", "");
    try {
      const reviewEmailAvailable =
        settings?.review_email_available === true;
      const nextSettings = await apiRequest(
        `/projects/${projectId}/graph-draft-batch-settings`,
        {
          body: {
            cadence_minutes: Number(cadenceMinutes),
            email_notifications_enabled:
              reviewEmailAvailable && emailNotificationsEnabled,
            enabled,
            notification_email:
              reviewEmailAvailable && emailNotificationsEnabled
                ? notificationEmail.trim() || null
                : null,
            run_at_local_time: runAtLocalTime,
            timezone_name: timezoneName,
          },
          method: "PATCH",
          token,
        }
      );
      if (currentProjectIdRef.current !== savedProjectId) {
        // The form moved to another project (or unmounted) while this save
        // was in flight: its result belongs to the previous project, so it
        // must neither populate nor report success into the current context.
        return;
      }
      setSettings(nextSettings);
      onSaved(nextSettings);
      setFlash("Daily review schedule updated.");
    } catch (err) {
      setFlash("", err.message || "Failed to update daily review timing.");
    } finally {
      setBusy(false);
    }
  }

  const runNowDisabled = !canManage || !projectId || loading;
  // Editing and saving are only possible once the current project's settings
  // have loaded, so a failed load can never PATCH values it did not return.
  const disabled = runNowDisabled || !settings;
  const reviewEmailAvailable = settings?.review_email_available === true;

  return (
    <form className="form" onSubmit={saveSettings}>
      <label className="inline toggle-row">
        <input
          type="checkbox"
          checked={enabled}
          disabled={disabled}
          onChange={(event) => setEnabled(event.target.checked)}
        />
        Enabled
      </label>
      <label>
        Cadence
        <select
          value={cadenceMinutes}
          disabled={disabled}
          onChange={(event) => setCadenceMinutes(event.target.value)}
        >
          {BATCH_CADENCE_OPTIONS.map((option) => (
            <option key={option.value} value={option.value}>
              {option.label}
            </option>
          ))}
        </select>
      </label>
      <label>
        Local run time
        <input
          type="time"
          value={runAtLocalTime}
          disabled={disabled}
          onChange={(event) => setRunAtLocalTime(event.target.value)}
        />
      </label>
      <div className="inline">
        <button
          type="button"
          className="btn-secondary"
          aria-pressed={runAtLocalTime === "18:00"}
          disabled={disabled}
          onClick={() => setRunAtLocalTime("18:00")}
        >
          Evening (6:00 PM)
        </button>
        <button
          type="button"
          className="btn-secondary"
          aria-pressed={runAtLocalTime === "06:00"}
          disabled={disabled}
          onClick={() => setRunAtLocalTime("06:00")}
        >
          Morning (6:00 AM)
        </button>
      </div>
      <p className="subtle">
        This is when Lab Tracker drafts the review queue. You still decide what
        enters the research graph.
      </p>
      <label>
        Time zone
        <input
          value={timezoneName}
          disabled={disabled}
          onChange={(event) => setTimezoneName(event.target.value)}
          placeholder="America/New_York"
        />
      </label>
      {settings && !reviewEmailAvailable ? (
        <p className="warn">
          Email cues are unavailable because this host has not configured
          delivery. Saving this schedule cannot opt you into an undeliverable
          notification backlog.
        </p>
      ) : (
        <>
          <label className="inline toggle-row">
            <input
              type="checkbox"
              checked={emailNotificationsEnabled}
              disabled={disabled || !reviewEmailAvailable}
              onChange={(event) =>
                setEmailNotificationsEnabled(event.target.checked)
              }
            />
            Email me when a review is ready
          </label>
          <label>
            Notification email
            <input
              type="email"
              value={notificationEmail}
              disabled={
                disabled ||
                !reviewEmailAvailable ||
                !emailNotificationsEnabled
              }
              required={
                reviewEmailAvailable && emailNotificationsEnabled
              }
              autoComplete="email"
              onChange={(event) => setNotificationEmail(event.target.value)}
              placeholder="name@example.edu"
            />
          </label>
          <p className="subtle">
            The message is a generic, privacy-preserving cue. It does not
            include project names or research content.
          </p>
        </>
      )}
      {settings?.next_run_at ? (
        <p className="subtle">Next run: {formatDate(settings.next_run_at)}</p>
      ) : null}
      <div className="inline">
        <button className="btn-primary" disabled={disabled}>
          {loading ? "Loading…" : "Save cadence"}
        </button>
        {onRunNow ? (
          <button
            type="button"
            className="btn-secondary"
            disabled={runNowDisabled}
            onClick={onRunNow}
          >
            Run now
          </button>
        ) : null}
      </div>
    </form>
  );
}

export { BATCH_CADENCE_OPTIONS, DailyReviewScheduleForm, detectedTimeZone };
