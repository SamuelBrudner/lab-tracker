import * as React from "react";

import { apiRequest, buildApiPath } from "../shared/api.js";
import { formatDate } from "../shared/formatters.js";

const { useCallback, useEffect, useState } = React;

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
  userId = "",
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

  const loadSettings = useCallback(async () => {
    if (!projectId) {
      setSettings(null);
      return;
    }
    setLoading(true);
    try {
      const path = buildApiPath(
        `/projects/${projectId}/graph-draft-batch-settings`,
        userId ? { user_id: userId } : {}
      );
      const nextSettings = await apiRequest(path, { token });
      setSettings(nextSettings);
      setEnabled(Boolean(nextSettings.enabled));
      setCadenceMinutes(String(nextSettings.cadence_minutes || 1440));
      setRunAtLocalTime(nextSettings.run_at_local_time || "18:00");
      setTimezoneName(nextSettings.timezone_name || detectedTimeZone());
      setEmailNotificationsEnabled(
        Boolean(nextSettings.email_notifications_enabled)
      );
      setNotificationEmail(nextSettings.notification_email || "");
    } catch (err) {
      setSettings(null);
      setFlash("", err.message || "Failed to load daily review timing.");
    } finally {
      setLoading(false);
    }
  }, [projectId, setFlash, token, userId]);

  useEffect(() => {
    loadSettings();
  }, [loadSettings]);

  async function saveSettings(event) {
    event.preventDefault();
    if (!projectId || !canManage) {
      return;
    }
    setBusy(true);
    setFlash("", "");
    try {
      const nextSettings = await apiRequest(
        `/projects/${projectId}/graph-draft-batch-settings`,
        {
          body: {
            cadence_minutes: Number(cadenceMinutes),
            email_notifications_enabled: emailNotificationsEnabled,
            enabled,
            notification_email: notificationEmail.trim() || null,
            run_at_local_time: runAtLocalTime,
            timezone_name: timezoneName,
            ...(userId ? { user_id: userId } : {}),
          },
          method: "PATCH",
          token,
        }
      );
      setSettings(nextSettings);
      onSaved(nextSettings);
      setFlash("Daily review schedule updated.");
    } catch (err) {
      setFlash("", err.message || "Failed to update daily review timing.");
    } finally {
      setBusy(false);
    }
  }

  const disabled = !canManage || !projectId || loading;

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
      <label className="inline toggle-row">
        <input
          type="checkbox"
          checked={emailNotificationsEnabled}
          disabled={disabled}
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
          disabled={disabled || !emailNotificationsEnabled}
          required={emailNotificationsEnabled}
          autoComplete="email"
          onChange={(event) => setNotificationEmail(event.target.value)}
          placeholder="name@example.edu"
        />
      </label>
      <p className="subtle">
        The message is a generic, privacy-preserving cue. It does not include
        project names or research content.
      </p>
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
            disabled={disabled}
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
