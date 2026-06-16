import * as React from "react";
import { render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { TOKEN_STORAGE_KEY } from "../shared/constants.js";
import { apiResponse, errorResponse, installFetchMock } from "../test/utils.js";
import { useAuthSession } from "./useAuthSession.js";

function AuthHarness({ replace = vi.fn(), setBusy = vi.fn(), setFlash = vi.fn() }) {
  const session = useAuthSession({ replace, setBusy, setFlash });
  return (
    <>
      <span data-testid="token">{session.token}</span>
      <span data-testid="auth-mode">{session.authMode}</span>
      <span data-testid="bootstrap-token">{session.authBootstrapToken}</span>
    </>
  );
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
    expect(screen.getByTestId("token")).toHaveTextContent("");
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
    expect(screen.getByTestId("auth-mode")).toHaveTextContent("setup");
  });
});
