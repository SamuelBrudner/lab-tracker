import * as React from "react";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { apiRequest } from "../shared/api.js";
import {
  TOKEN_EXPIRES_AT_STORAGE_KEY,
  TOKEN_STORAGE_KEY,
} from "../shared/constants.js";
import { apiResponse, errorResponse, installFetchMock } from "../test/utils.js";
import { MIN_REFRESH_DELAY_MS, useAuthSession } from "./useAuthSession.js";

const USER = {
  created_at: "2026-06-18T12:00:00Z",
  role: "admin",
  user_id: "00000000-0000-0000-0000-000000000001",
  username: "sam",
};

function noop() {}

afterEach(() => {
  vi.useRealTimers();
});

function AuthHarness({
  replace = noop,
  setBusy = noop,
  setFlash = noop,
  withProbe = false,
}) {
  const session = useAuthSession({ replace, setBusy, setFlash });
  async function probe() {
    try {
      await apiRequest("/protected", { token: session.token });
    } catch {
      // The session hook handles auth rejection via the API-layer event.
    }
  }
  return (
    <>
      <span data-testid="token">{session.token}</span>
      <span data-testid="expires-at">{session.tokenExpiresAt}</span>
      <span data-testid="auth-mode">{session.authMode}</span>
      <span data-testid="bootstrap-token">{session.authBootstrapToken}</span>
      {withProbe ? (
        <button type="button" onClick={probe}>
          Probe API
        </button>
      ) : null}
    </>
  );
}

async function flushAuthEffects() {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
}

function captureRefreshTimers() {
  const timers = [];
  const realSetTimeout = window.setTimeout.bind(window);
  const realClearTimeout = window.clearTimeout.bind(window);
  const capturedTimerIds = new Set();
  vi.spyOn(window, "setTimeout").mockImplementation((callback, delay, ...args) => {
    if (callback?.name === "refreshSession") {
      timers.push({ callback, delay });
      const timerId = 1000 + timers.length;
      capturedTimerIds.add(timerId);
      return timerId;
    }
    return realSetTimeout(callback, delay, ...args);
  });
  vi.spyOn(window, "clearTimeout").mockImplementation((timerId) => {
    if (capturedTimerIds.has(timerId)) {
      capturedTimerIds.delete(timerId);
      return undefined;
    }
    return realClearTimeout(timerId);
  });
  return timers;
}

async function runRefreshTimer(timer) {
  await act(async () => {
    await timer.callback();
  });
  await flushAuthEffects();
}

describe("useAuthSession", () => {
  it("preserves a saved token when boot session restore fails offline", async () => {
    localStorage.setItem(TOKEN_STORAGE_KEY, "stored-token");
    const setBusy = vi.fn();
    const setFlash = vi.fn();
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new TypeError("Network unavailable");
      })
    );

    render(<AuthHarness setBusy={setBusy} setFlash={setFlash} />);

    await waitFor(() => expect(setBusy).toHaveBeenLastCalledWith(false));
    expect(screen.getByTestId("token")).toHaveTextContent("stored-token");
    expect(localStorage.getItem(TOKEN_STORAGE_KEY)).toBe("stored-token");
    expect(setFlash).toHaveBeenCalledWith("", "Network unavailable");
  });

  it("clears a saved token when the server rejects it", async () => {
    localStorage.setItem(TOKEN_STORAGE_KEY, "expired-token");
    const setBusy = vi.fn();
    installFetchMock([
      {
        match: "/auth/me",
        response: errorResponse("Invalid token.", 401),
      },
    ]);

    render(<AuthHarness setBusy={setBusy} />);

    await waitFor(() => expect(localStorage.getItem(TOKEN_STORAGE_KEY)).toBeNull());
    expect(localStorage.getItem(TOKEN_EXPIRES_AT_STORAGE_KEY)).toBeNull();
    expect(screen.getByTestId("token")).toHaveTextContent("");
  });

  it("refreshes a saved token before its server expiry", async () => {
    const nearExpiry = new Date(Date.now() + 4 * 60 * 1000).toISOString();
    const refreshedExpiry = new Date(Date.now() + 60 * 60 * 1000).toISOString();
    localStorage.setItem(TOKEN_STORAGE_KEY, "stored-token");
    localStorage.setItem(TOKEN_EXPIRES_AT_STORAGE_KEY, nearExpiry);
    const setBusy = vi.fn();
    const refreshTimers = captureRefreshTimers();
    const fetchMock = installFetchMock([
      {
        match: "/auth/me",
        response: apiResponse(USER, 200, { auth_enabled: true }),
      },
      {
        match: "/auth/refresh",
        method: "POST",
        response: apiResponse({
          access_token: "refreshed-token",
          expires_at: refreshedExpiry,
          user: USER,
        }),
      },
    ]);

    render(<AuthHarness setBusy={setBusy} />);

    await flushAuthEffects();
    expect(setBusy).toHaveBeenLastCalledWith(false);
    expect(refreshTimers).toHaveLength(1);
    expect(refreshTimers[0].delay).toBe(MIN_REFRESH_DELAY_MS);
    await runRefreshTimer(refreshTimers[0]);
    expect(localStorage.getItem(TOKEN_STORAGE_KEY)).toBe("refreshed-token");
    expect(localStorage.getItem(TOKEN_EXPIRES_AT_STORAGE_KEY)).toBe(refreshedExpiry);
    expect(screen.getByTestId("expires-at")).toHaveTextContent(refreshedExpiry);
    expect(fetchMock).toHaveBeenCalledWith(
      "/auth/refresh",
      expect.objectContaining({
        headers: expect.objectContaining({
          Authorization: "Bearer stored-token",
        }),
        method: "POST",
      })
    );
  });

  it("reschedules a short refreshed token with a bounded delay", async () => {
    const nearExpiry = new Date(Date.now() + 4 * 60 * 1000).toISOString();
    const shortRefreshedExpiry = new Date(Date.now() + 2 * 60 * 1000).toISOString();
    localStorage.setItem(TOKEN_STORAGE_KEY, "stored-token");
    localStorage.setItem(TOKEN_EXPIRES_AT_STORAGE_KEY, nearExpiry);
    const setBusy = vi.fn();
    const refreshTimers = captureRefreshTimers();
    const fetchMock = installFetchMock([
      {
        match: "/auth/me",
        response: apiResponse(USER, 200, { auth_enabled: true }),
      },
      {
        match: "/auth/refresh",
        method: "POST",
        response: apiResponse({
          access_token: "short-refresh-token",
          expires_at: shortRefreshedExpiry,
          user: USER,
        }),
      },
    ]);

    render(<AuthHarness setBusy={setBusy} />);

    await flushAuthEffects();
    expect(setBusy).toHaveBeenLastCalledWith(false);
    expect(refreshTimers).toHaveLength(1);
    expect(refreshTimers[0].delay).toBe(MIN_REFRESH_DELAY_MS);
    await runRefreshTimer(refreshTimers[0]);
    expect(localStorage.getItem(TOKEN_STORAGE_KEY)).toBe("short-refresh-token");
    expect(refreshTimers).toHaveLength(2);
    expect(refreshTimers[1].delay).toBe(MIN_REFRESH_DELAY_MS);
    expect(fetchMock.mock.calls.filter(([url]) => url === "/auth/refresh")).toHaveLength(1);
  });

  it("clears the current token when a later API request returns an auth 401", async () => {
    localStorage.setItem(TOKEN_STORAGE_KEY, "stored-token");
    localStorage.setItem(
      TOKEN_EXPIRES_AT_STORAGE_KEY,
      new Date(Date.now() + 60 * 60 * 1000).toISOString()
    );
    const setBusy = vi.fn();
    const setFlash = vi.fn();
    installFetchMock([
      {
        match: "/auth/me",
        response: apiResponse(USER, 200, { auth_enabled: true }),
      },
      {
        match: "/protected",
        response: errorResponse("Token has expired.", 401),
      },
    ]);

    render(<AuthHarness setBusy={setBusy} setFlash={setFlash} withProbe />);

    await waitFor(() => expect(setBusy).toHaveBeenLastCalledWith(false));
    fireEvent.click(screen.getByRole("button", { name: "Probe API" }));

    await waitFor(() => expect(localStorage.getItem(TOKEN_STORAGE_KEY)).toBeNull());
    expect(localStorage.getItem(TOKEN_EXPIRES_AT_STORAGE_KEY)).toBeNull();
    expect(screen.getByTestId("token")).toHaveTextContent("");
    expect(setFlash).toHaveBeenLastCalledWith("", "Your session expired. Please sign in again.");
  });

  it("keeps the token when a later API request returns a permission 401", async () => {
    localStorage.setItem(TOKEN_STORAGE_KEY, "stored-token");
    const setBusy = vi.fn();
    const setFlash = vi.fn();
    const fetchMock = installFetchMock([
      {
        match: "/auth/me",
        response: apiResponse(USER, 200, { auth_enabled: true }),
      },
      {
        match: "/protected",
        response: errorResponse("Project access required.", 401),
      },
    ]);

    render(<AuthHarness setBusy={setBusy} setFlash={setFlash} withProbe />);

    await waitFor(() => expect(setBusy).toHaveBeenLastCalledWith(false));
    fireEvent.click(screen.getByRole("button", { name: "Probe API" }));

    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        "/protected",
        expect.objectContaining({ method: "GET" })
      )
    );
    expect(localStorage.getItem(TOKEN_STORAGE_KEY)).toBe("stored-token");
    expect(screen.getByTestId("token")).toHaveTextContent("stored-token");
    expect(setFlash).not.toHaveBeenCalledWith(
      "",
      "Your session expired. Please sign in again."
    );
  });

  it("loads a surfaced first-admin token into setup mode", async () => {
    const setBusy = vi.fn();
    installFetchMock([
      {
        match: "/auth/bootstrap-status",
        response: apiResponse({
          bootstrap_admin_configured: true,
          bootstrap_token: "bootstrap-secret",
          bootstrap_token_warning: null,
          first_admin_available: true,
          has_users: false,
        }),
      },
      {
        match: "/auth/me",
        response: errorResponse("Authentication required.", 401),
      },
    ]);

    render(<AuthHarness setBusy={setBusy} />);

    await waitFor(() =>
      expect(screen.getByTestId("bootstrap-token")).toHaveTextContent("bootstrap-secret")
    );
    await waitFor(() =>
      expect(screen.getByTestId("auth-mode")).toHaveTextContent("setup")
    );
  });
});
