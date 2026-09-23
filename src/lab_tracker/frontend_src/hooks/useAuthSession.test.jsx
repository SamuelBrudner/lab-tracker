import * as React from "react";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AUTH_REJECTED_EVENT, apiRequest } from "../shared/api.js";
import { createAuthStorage } from "../shared/auth-storage.js";
import {
  TOKEN_EXPIRES_AT_STORAGE_KEY,
  TOKEN_STORAGE_KEY,
} from "../shared/constants.js";
import { apiResponse, errorResponse, installFetchMock } from "../test/utils.js";
import { MIN_REFRESH_DELAY_MS, useAuthSession } from "./useAuthSession.js";
import { DRAFT_KEY_PREFIX, useLocalDraft } from "./useLocalDraft.js";

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
  storage = undefined,
  withProbe = false,
}) {
  const session = useAuthSession({ replace, setBusy, setFlash, storage });
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
      <span data-testid="degraded">{String(session.persistenceDegraded)}</span>
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

  it("clears a paired-device session when its revoked device token returns 401", async () => {
    localStorage.setItem(TOKEN_STORAGE_KEY, "ltd_revoked-device-secret");
    const setBusy = vi.fn();
    const setFlash = vi.fn();
    installFetchMock([
      {
        match: "/auth/me",
        response: apiResponse(USER, 200, { auth_enabled: true }),
      },
      {
        match: "/protected",
        response: errorResponse("Invalid device token.", 401),
      },
    ]);

    render(<AuthHarness setBusy={setBusy} setFlash={setFlash} withProbe />);

    await waitFor(() => expect(setBusy).toHaveBeenLastCalledWith(false));
    fireEvent.click(screen.getByRole("button", { name: "Probe API" }));

    await waitFor(() => expect(localStorage.getItem(TOKEN_STORAGE_KEY)).toBeNull());
    expect(screen.getByTestId("token")).toHaveTextContent("");
  });

  it("ignores an auth rejection that does not name the current token", async () => {
    localStorage.setItem(TOKEN_STORAGE_KEY, "stored-token");
    const setBusy = vi.fn();
    const setFlash = vi.fn();
    installFetchMock([
      {
        match: "/auth/me",
        response: apiResponse(USER, 200, { auth_enabled: true }),
      },
    ]);

    render(<AuthHarness setBusy={setBusy} setFlash={setFlash} />);
    await waitFor(() => expect(setBusy).toHaveBeenLastCalledWith(false));

    act(() => {
      for (const token of ["", "some-other-token"]) {
        window.dispatchEvent(
          new CustomEvent(AUTH_REJECTED_EVENT, {
            detail: { message: "Enrollment offer has expired.", status: 401, token },
          })
        );
      }
    });

    expect(localStorage.getItem(TOKEN_STORAGE_KEY)).toBe("stored-token");
    expect(screen.getByTestId("token")).toHaveTextContent("stored-token");
    expect(setFlash).not.toHaveBeenCalledWith(
      "",
      "Your session expired. Please sign in again."
    );
  });

  it("keeps the token when a later API request returns a permission 403", async () => {
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
        response: errorResponse("Project contributor access required.", 403),
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

  it("keeps the token and expiry when a protected read returns an opaque 404", async () => {
    const expiresAt = new Date(Date.now() + 60 * 60 * 1000).toISOString();
    localStorage.setItem(TOKEN_STORAGE_KEY, "stored-token");
    localStorage.setItem(TOKEN_EXPIRES_AT_STORAGE_KEY, expiresAt);
    const setBusy = vi.fn();
    const setFlash = vi.fn();
    const fetchMock = installFetchMock([
      {
        match: "/auth/me",
        response: apiResponse(USER, 200, { auth_enabled: true }),
      },
      {
        match: "/protected",
        response: errorResponse("Project does not exist.", 404),
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
    expect(localStorage.getItem(TOKEN_EXPIRES_AT_STORAGE_KEY)).toBe(expiresAt);
    expect(screen.getByTestId("token")).toHaveTextContent("stored-token");
    expect(screen.getByTestId("expires-at")).toHaveTextContent(expiresAt);
    expect(setFlash).not.toHaveBeenCalledWith(
      "",
      "Your session expired. Please sign in again."
    );
  });

  it("keeps a valid session and reports degraded persistence when storage writes throw", async () => {
    // Backing store rejects every write/remove (quota/SecurityError), but the
    // live session must survive and the degradation must be surfaced, not crash.
    const throwingStorage = createAuthStorage({
      getItem: () => "stored-token",
      setItem: () => {
        throw new Error("QuotaExceededError");
      },
      removeItem: () => {
        throw new Error("SecurityError");
      },
    });
    const setBusy = vi.fn();
    installFetchMock([
      {
        match: "/auth/me",
        response: apiResponse(USER, 200, { auth_enabled: true }),
      },
    ]);

    render(<AuthHarness setBusy={setBusy} storage={throwingStorage} />);

    await waitFor(() => expect(setBusy).toHaveBeenLastCalledWith(false));
    expect(screen.getByTestId("token")).toHaveTextContent("stored-token");
    await waitFor(() => expect(screen.getByTestId("degraded")).toHaveTextContent("true"));
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

describe("useAuthSession local drafts across users", () => {
  const PREVIOUS_USER_ID = "00000000-0000-0000-0000-00000000000a";
  const DRAFT_KEY = "note:project-1";
  const NEXT_USER = {
    created_at: "2026-06-18T12:00:00Z",
    role: "editor",
    user_id: "00000000-0000-0000-0000-00000000000b",
    username: "next-person",
  };

  function DraftProbe() {
    const draft = useLocalDraft({ key: DRAFT_KEY, value: "" });
    return <span data-testid="recovered">{draft.recoveredValue ?? "(none)"}</span>;
  }

  function SignInHarness() {
    const session = useAuthSession({ replace: noop, setBusy: noop, setFlash: noop });
    return (
      <form onSubmit={session.handleAuthSubmit}>
        <input
          aria-label="Username"
          value={session.authUsername}
          onChange={(event) => session.setAuthUsername(event.target.value)}
        />
        <input
          aria-label="Password"
          value={session.authPassword}
          onChange={(event) => session.setAuthPassword(event.target.value)}
        />
        <button type="submit">Sign in</button>
        {session.user ? <DraftProbe /> : null}
      </form>
    );
  }

  function leavePreviousUsersDraft() {
    // The previous person's session expired (no sign-out), leaving their text.
    localStorage.setItem("lab-tracker:draft-owner", PREVIOUS_USER_ID);
    localStorage.setItem(
      `${DRAFT_KEY_PREFIX}${DRAFT_KEY}`,
      JSON.stringify({ savedAt: 1, value: "Previous person's unsent notes" })
    );
  }

  function signIn(user) {
    installFetchMock([
      {
        match: "/auth/me",
        response: [
          errorResponse("Authentication required.", 401),
          apiResponse(user, 200, { auth_enabled: true }),
        ],
      },
      {
        match: "/auth/login",
        method: "POST",
        response: apiResponse({
          access_token: "fresh-token",
          expires_at: new Date(Date.now() + 60 * 60 * 1000).toISOString(),
          token_type: "bearer",
          user,
        }),
      },
    ]);
    render(<SignInHarness />);
    fireEvent.change(screen.getByLabelText("Username"), { target: { value: user.username } });
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "secret-pass" } });
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
  }

  it("does not offer a previous user's draft to the next person who signs in", async () => {
    leavePreviousUsersDraft();

    signIn(NEXT_USER);

    await screen.findByTestId("recovered");
    await flushAuthEffects();
    expect(screen.getByTestId("recovered")).toHaveTextContent("(none)");
    expect(localStorage.getItem(`${DRAFT_KEY_PREFIX}${DRAFT_KEY}`)).toBeNull();
    expect(localStorage.getItem("lab-tracker:draft-owner")).toBe(NEXT_USER.user_id);
  });

  it("still offers the same user's draft after their session expired", async () => {
    leavePreviousUsersDraft();

    signIn({ ...NEXT_USER, user_id: PREVIOUS_USER_ID, username: "previous-person" });

    await waitFor(() =>
      expect(screen.getByTestId("recovered")).toHaveTextContent(
        "Previous person's unsent notes"
      )
    );
  });

  it("drops a draft whose owner is unknown when a user resolves from a saved token", async () => {
    localStorage.setItem(TOKEN_STORAGE_KEY, "saved-token");
    localStorage.setItem(
      `${DRAFT_KEY_PREFIX}${DRAFT_KEY}`,
      JSON.stringify({ savedAt: 1, value: "Unattributed text" })
    );
    installFetchMock([
      { match: "/auth/me", response: apiResponse(NEXT_USER, 200, { auth_enabled: true }) },
    ]);

    render(<SignInHarness />);

    await screen.findByTestId("recovered");
    await flushAuthEffects();
    expect(screen.getByTestId("recovered")).toHaveTextContent("(none)");
    expect(localStorage.getItem(`${DRAFT_KEY_PREFIX}${DRAFT_KEY}`)).toBeNull();
  });
});
