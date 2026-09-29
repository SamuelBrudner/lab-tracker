import * as React from "react";

import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { apiResponse, errorResponse, installFetchMock } from "../../test/utils.js";
import { HandsFreeShortcutPanel, shortcutUrl } from "./HandsFreeShortcutPanel.jsx";

const PROJECTS = [
  { project_id: "project-1", name: "Rig project" },
  { project_id: "project-2", name: "Imaging" },
];
const SECRET = "ldev_shortcut-secret";

function installMintRoutes({ fail = false } = {}) {
  return installFetchMock([
    {
      method: "POST",
      match: "/auth/devices/enrollment",
      response: apiResponse(
        {
          enrollment_id: "enrollment-1",
          enrollment_qr_svg: "<svg></svg>",
          enrollment_url: "http://lab.example/app/enroll?offer=lpair_offer",
          expires_at: "2026-09-28T10:05:00Z",
          offer_token: "lpair_offer",
        },
        201
      ),
    },
    {
      method: "POST",
      match: "/auth/devices/consume",
      response: fail
        ? errorResponse("Offer expired.", 400)
        : apiResponse(
            {
              created_at: "2026-09-28T10:00:00Z",
              device_token_id: "device-9",
              label: "Lab note shortcut",
              secret: SECRET,
            },
            201
          ),
    },
  ]);
}

function renderPanel(overrides = {}) {
  const props = {
    token: "session-token",
    canWrite: true,
    projects: PROJECTS,
    selectedProjectId: "project-2",
    setFlash: vi.fn(),
    onCredentialCreated: vi.fn(async () => undefined),
    ...overrides,
  };
  return { props, ...render(<HandsFreeShortcutPanel {...props} />) };
}

describe("HandsFreeShortcutPanel", () => {
  it("shows the exact request for the selected project with the latest session", () => {
    installMintRoutes();
    renderPanel();

    const url = `${window.location.origin}/notes/voice-capture?project_id=project-2&session_id=latest`;
    expect(shortcutUrl("project-2")).toBe(url);
    expect(screen.getByText(url)).toBeInTheDocument();
    expect(screen.getByText("POST")).toBeInTheDocument();
    expect(screen.getByText(/Authorization: Bearer <shortcut credential>/)).toBeInTheDocument();
    expect(screen.getByText(/Content-Type: audio\/mp4/)).toBeInTheDocument();
    // No secret is on the page until one is minted here.
    expect(screen.queryByText(/ldev_/)).not.toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("Project"), { target: { value: "project-1" } });
    expect(screen.getByText(shortcutUrl("project-1"))).toBeInTheDocument();
  });

  it("mints a dedicated paired-device credential and shows it once with its warnings", async () => {
    const fetchMock = installMintRoutes();
    const { props } = renderPanel();

    fireEvent.change(screen.getByLabelText("Credential name"), {
      target: { value: "Lab note shortcut" },
    });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Create shortcut credential" }));
    });

    const issued = await screen.findByRole("region", { name: "Shortcut credential" });
    expect(within(issued).getByText(SECRET)).toBeInTheDocument();
    expect(within(issued).getByText(/shown only once/)).toBeInTheDocument();
    expect(within(issued).getByText(/anyone holding it can read/)).toBeInTheDocument();
    expect(props.onCredentialCreated).toHaveBeenCalledTimes(1);

    const [enrollmentCall, consumeCall] = fetchMock.mock.calls;
    expect(enrollmentCall[0]).toBe("/auth/devices/enrollment");
    expect(enrollmentCall[1].headers.Authorization).toBe("Bearer session-token");
    expect(consumeCall[0]).toBe("/auth/devices/consume");
    expect(JSON.parse(consumeCall[1].body)).toEqual({
      label: "Lab note shortcut",
      offer_token: "lpair_offer",
    });

    fireEvent.click(within(issued).getByRole("button", { name: /saved it/ }));
    expect(screen.queryByText(SECRET)).not.toBeInTheDocument();
  });

  it("reports a failed mint without showing anything secret", async () => {
    installMintRoutes({ fail: true });
    const { props } = renderPanel();

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Create shortcut credential" }));
    });

    await waitFor(() => expect(props.setFlash).toHaveBeenCalledWith("", "Offer expired."));
    expect(screen.queryByRole("region", { name: "Shortcut credential" })).not.toBeInTheDocument();
    expect(props.onCredentialCreated).not.toHaveBeenCalled();
  });

  it("copies the URL", async () => {
    installMintRoutes();
    const writeText = vi.fn(async () => undefined);
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText } });
    const { props } = renderPanel();

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Copy URL" }));
    });

    expect(writeText).toHaveBeenCalledWith(shortcutUrl("project-2"));
    expect(props.setFlash).toHaveBeenCalledWith("Shortcut URL copied.");
    delete navigator.clipboard;
  });

  it("cannot mint without a signed-in person", () => {
    installMintRoutes();
    renderPanel({ canWrite: false });

    expect(screen.getByRole("button", { name: "Create shortcut credential" })).toBeDisabled();
  });
});
