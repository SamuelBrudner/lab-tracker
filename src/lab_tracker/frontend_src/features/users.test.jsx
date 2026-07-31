import * as React from "react";

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { apiResponse, installFetchMock } from "../test/utils.js";
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
