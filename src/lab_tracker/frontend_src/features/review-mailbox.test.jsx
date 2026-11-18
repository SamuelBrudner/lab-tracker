import * as React from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { expect, it } from "vitest";

import { apiResponse, installFetchMock } from "../test/utils.js";
import { ReviewMailbox } from "./review-mailbox.jsx";

it("verifies the login mailbox, enables proposal review and lets the scientist turn it off", async () => {
  const endpoint = "/auth/review-mailbox";
  const login = "scientist@example.edu";
  const initial = { available: true, enabled: false, login_email: login, pending_email: null };
  const requests = installFetchMock([
    { match: endpoint, response: apiResponse(initial) },
    { match: endpoint + "/challenge", method: "POST", response: apiResponse({ ...initial, pending_email: login }) },
    { match: endpoint + "/confirm", method: "POST", response: apiResponse({ ...initial, enabled: true }) },
    { match: endpoint, method: "DELETE", response: apiResponse(initial) },
  ]);
  render(<ReviewMailbox token="session" />);
  fireEvent.click(await screen.findByRole("button", { name: "Review proposals by email" }));
  expect(await screen.findByText("Login email: " + login)).toBeInTheDocument();
  expect(screen.queryByLabelText("New login email")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "Send verification code" }));
  fireEvent.change(await screen.findByLabelText("Verification code"), { target: { value: "proof" } });
  fireEvent.click(screen.getByRole("button", { name: "Verify and enable email review" }));
  const disable = await screen.findByRole("button", { name: "Turn off proposal review by email" });
  expect(requests).toHaveBeenCalledWith(endpoint + "/confirm", expect.objectContaining({
    method: "POST", body: JSON.stringify({ code: "proof" }),
  }));
  fireEvent.click(disable);
  await waitFor(() => expect(screen.getByRole("button", { name: "Send verification code" })).toBeInTheDocument());
});
