import * as React from "react";

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { apiResponse, errorResponse, installFetchMock } from "../../test/utils.js";
import { EmailCapturePanel } from "./EmailCapturePanel.jsx";

const ADDRESS = "capture+p1-abcdefghij0123456789@lab.example";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("EmailCapturePanel", () => {
  it("shows the person's private capture address and copies it", async () => {
    installFetchMock([
      {
        match: "/projects/project-1/capture-address",
        response: apiResponse({
          accepted_senders: ["alice@lab.example"],
          address: ADDRESS,
          project_id: "project-1",
        }),
      },
    ]);
    const writeText = vi.fn(async () => undefined);
    vi.stubGlobal("navigator", { ...navigator, clipboard: { writeText } });

    render(<EmailCapturePanel token="token-1" selectedProjectId="project-1" />);

    expect(await screen.findByText(ADDRESS)).toBeInTheDocument();
    expect(screen.getByText("Accepted from: alice@lab.example")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Copy address" }));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith(ADDRESS));
    expect(await screen.findByText("Address copied.")).toBeInTheDocument();
  });

  it("says why there is no address when the server has none for this person", async () => {
    installFetchMock([
      {
        match: "/projects/project-1/capture-address",
        response: errorResponse("Email capture is not configured on this server.", 404),
      },
    ]);

    render(<EmailCapturePanel token="token-1" selectedProjectId="project-1" />);

    expect(
      await screen.findByText("Email capture is not configured on this server.")
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Copy address" })).not.toBeInTheDocument();
  });

  it("renders nothing until a project is selected", () => {
    const fetchMock = installFetchMock([]);

    const { container } = render(<EmailCapturePanel token="token-1" selectedProjectId="" />);

    expect(container).toBeEmptyDOMElement();
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
