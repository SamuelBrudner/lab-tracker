import * as React from "react";

import { render, screen } from "@testing-library/react";
import { vi } from "vitest";

import { DevicesPage } from "./devices.jsx";
import { apiResponse, installFetchMock } from "../test/utils.js";

it("lets a paired device manage push without enumerating the user's other devices", async () => {
  const fetchMock = installFetchMock([
    {
      match: "/notifications/push/config",
      response: apiResponse({ enabled: true, application_server_key: "key" }),
    },
  ]);

  render(
    <DevicesPage
      token="ldev_paired-device"
      canWrite={true}
      navigate={vi.fn()}
      setFlash={vi.fn()}
    />
  );

  expect(
    await screen.findByText("This browser does not support Web Push notifications.")
  ).toBeInTheDocument();
  expect(fetchMock.mock.calls.some(([url]) => url === "/auth/devices")).toBe(false);
  expect(screen.queryByRole("button", { name: "Add a new device" })).not.toBeInTheDocument();
});
