import * as React from "react";

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { apiResponse, errorResponse, installFetchMock } from "../test/utils.js";
import { UsersPage } from "./users.jsx";

describe("UsersPage invitations", () => {
  it("creates an email-ready invitation link", async () => {
    const setBusy = vi.fn();
    const setFlash = vi.fn();
    const fetchMock = installFetchMock([
      {
        match: "/auth/users?limit=200",
        response: apiResponse([], 200, { limit: 200, offset: 0, total: 0 }),
      },
      {
        match: "/auth/invitations?limit=200",
        response: [
          apiResponse([], 200, { limit: 200, offset: 0, total: 0 }),
          apiResponse(
            [
              {
                created_at: "2026-06-16T12:00:00Z",
                email: "member@example.org",
                expires_at: "2026-06-22T12:00:00Z",
                invitation_id: "11111111-1111-4111-8111-111111111111",
                role: "editor",
                status: "pending",
              },
            ],
            200,
            { limit: 200, offset: 0, total: 1 }
          ),
        ],
      },
      {
        match: "/auth/invitations",
        method: "POST",
        response: (request) => {
          expect(JSON.parse(request.init.body)).toEqual({
            email: "member@example.org",
            role: "editor",
          });
          return apiResponse(
            {
              created_at: "2026-06-16T12:00:00Z",
              email: "member@example.org",
              expires_at: "2026-06-22T12:00:00Z",
              invitation_id: "11111111-1111-4111-8111-111111111111",
              invite_url: "https://lab.example.org/app/#invite=signed-token",
              mailto_url: "mailto:member%40example.org?subject=Lab%20Tracker%20invitation",
              role: "editor",
              status: "pending",
            },
            201
          );
        },
      },
    ]);

    render(
      <UsersPage token="admin-token" canManageUsers setBusy={setBusy} setFlash={setFlash} />
    );

    fireEvent.change(screen.getByLabelText("Email"), {
      target: { value: "member@example.org" },
    });
    fireEvent.change(screen.getByLabelText("Global role"), {
      target: { value: "editor" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Create invite" }));

    await waitFor(() => expect(setFlash).toHaveBeenCalledWith("Invitation link created."));
    expect(
      screen.getByDisplayValue("https://lab.example.org/app/#invite=signed-token")
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Revoke invite" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Email invite" })).toHaveAttribute(
      "href",
      "mailto:member%40example.org?subject=Lab%20Tracker%20invitation"
    );
    expect(fetchMock).toHaveBeenCalledWith(
      "/auth/invitations",
      expect.objectContaining({
        headers: expect.objectContaining({ Authorization: "Bearer admin-token" }),
        method: "POST",
      })
    );
  });
});

describe("UsersPage password reset", () => {
  const USER_ID = "22222222-2222-4222-8222-222222222222";
  const listedUser = {
    created_at: "2026-06-16T12:00:00Z",
    role: "editor",
    user_id: USER_ID,
    username: "member",
  };

  function renderResetPage(patchResponse) {
    installFetchMock([
      {
        match: "/auth/users?limit=200",
        response: () => apiResponse([listedUser], 200, { limit: 200, offset: 0, total: 1 }),
      },
      {
        match: "/auth/invitations?limit=200",
        response: apiResponse([], 200, { limit: 200, offset: 0, total: 0 }),
      },
      { match: `/auth/users/${USER_ID}`, method: "PATCH", response: patchResponse },
    ]);
    const setFlash = vi.fn();
    render(<UsersPage token="admin-token" canManageUsers setBusy={vi.fn()} setFlash={setFlash} />);
    return setFlash;
  }

  it("keeps the typed password when the reset is rejected", async () => {
    const setFlash = renderResetPage(
      errorResponse("Password must be at least 12 characters.", 422)
    );

    const input = await screen.findByLabelText("New password");
    fireEvent.change(input, { target: { value: "short-pass" } });
    fireEvent.click(screen.getByRole("button", { name: "Reset password" }));

    await waitFor(() =>
      expect(setFlash).toHaveBeenLastCalledWith("", "Password must be at least 12 characters.")
    );
    expect(screen.getByLabelText("New password")).toHaveValue("short-pass");
  });

  it("clears the typed password once the reset succeeds", async () => {
    const setFlash = renderResetPage(apiResponse(listedUser));

    const input = await screen.findByLabelText("New password");
    fireEvent.change(input, { target: { value: "a-long-enough-password" } });
    fireEvent.click(screen.getByRole("button", { name: "Reset password" }));

    await waitFor(() => expect(setFlash).toHaveBeenLastCalledWith("Password reset."));
    await waitFor(() => expect(screen.getByLabelText("New password")).toHaveValue(""));
  });
});
