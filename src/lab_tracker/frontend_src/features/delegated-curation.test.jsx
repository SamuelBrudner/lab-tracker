import * as React from "react";

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { DelegatedCurationForm } from "./delegated-curation.jsx";
import { apiResponse, installFetchMock } from "../test/utils.js";

const PATH = "/projects/project-1/graph-draft-batch-settings/project-default";

function projectDefault(overrides = {}) {
  return {
    delegated_curation: "off",
    delegated_curation_granted_at: null,
    delegated_curation_granted_by: null,
    enabled: false,
    project_id: "project-1",
    settings_id: "settings-default",
    updated_at: "2026-09-01T09:00:00Z",
    user_id: null,
    ...overrides,
  };
}

function installGrantRoutes(initial, bodies) {
  return installFetchMock([
    { match: PATH, response: apiResponse(initial) },
    {
      match: PATH,
      method: "PATCH",
      response: (request) => {
        const body = JSON.parse(request.init.body);
        bodies.push(body);
        const widened = body.delegated_curation !== "off";
        return apiResponse(
          projectDefault({
            delegated_curation: body.delegated_curation,
            delegated_curation_granted_at: widened ? "2026-09-02T09:00:00Z" : null,
            delegated_curation_granted_by: widened ? "owner-1" : null,
          })
        );
      },
    },
  ]);
}

function renderForm(props = {}) {
  const setFlash = vi.fn();
  render(
    <DelegatedCurationForm
      token="token-1"
      projectId="project-1"
      setBusy={vi.fn()}
      setFlash={setFlash}
      {...props}
    />
  );
  return { setFlash };
}

describe("DelegatedCurationForm", () => {
  it("widens the grant only with the acknowledgement in the same request", async () => {
    const bodies = [];
    installGrantRoutes(projectDefault(), bodies);
    renderForm();

    const select = await screen.findByLabelText("Delegated curation");
    await waitFor(() => expect(select).not.toBeDisabled());
    expect(select).toHaveValue("off");
    expect(screen.queryByLabelText(/I understand that AI will change/)).not.toBeInTheDocument();

    fireEvent.change(select, { target: { value: "organize" } });
    const consent = await screen.findByLabelText(/I understand that AI will change/);
    const save = screen.getByRole("button", { name: "Save delegated curation" });
    expect(save).toBeDisabled();

    fireEvent.click(consent);
    await waitFor(() => expect(save).not.toBeDisabled());
    fireEvent.click(save);

    await waitFor(() => expect(bodies).toHaveLength(1));
    expect(bodies[0]).toEqual({
      delegated_curation: "organize",
      delegated_curation_acknowledged: true,
    });
    expect(await screen.findByText(/^Granted /)).toBeInTheDocument();
    // Widening again is a fresh act of consent: the box comes back unticked.
    fireEvent.change(screen.getByLabelText("Delegated curation"), {
      target: { value: "full" },
    });
    const again = await screen.findByLabelText(/I understand that AI will change/);
    expect(again).not.toBeChecked();
    expect(screen.getByRole("button", { name: "Save delegated curation" })).toBeDisabled();
  });

  it("turns the grant off without any acknowledgement", async () => {
    const bodies = [];
    installGrantRoutes(
      projectDefault({
        delegated_curation: "full",
        delegated_curation_granted_at: "2026-09-01T09:00:00Z",
        delegated_curation_granted_by: "owner-1",
      }),
      bodies
    );
    const { setFlash } = renderForm();

    const select = await screen.findByLabelText("Delegated curation");
    await waitFor(() => expect(select).toHaveValue("full"));
    expect(screen.getByText(/^Granted /)).toBeInTheDocument();

    fireEvent.change(select, { target: { value: "off" } });
    expect(screen.queryByLabelText(/I understand that AI will change/)).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Save delegated curation" }));

    await waitFor(() => expect(bodies).toEqual([{ delegated_curation: "off" }]));
    await waitFor(() => expect(screen.queryByText(/^Granted /)).not.toBeInTheDocument());
    expect(setFlash).toHaveBeenLastCalledWith(
      "Delegated curation is off; every proposal waits for a person again."
    );
  });

  it("ignores a late load for a previously selected project", async () => {
    let resolveFirst = null;
    installFetchMock([
      {
        match: PATH,
        response: () =>
          new Promise((resolve) => {
            resolveFirst = () =>
              resolve(apiResponse(projectDefault({ delegated_curation: "full" })));
          }),
      },
      {
        match: "/projects/project-2/graph-draft-batch-settings/project-default",
        response: apiResponse(projectDefault({ project_id: "project-2" })),
      },
    ]);
    const { rerender } = render(
      <DelegatedCurationForm
        token="token-1"
        projectId="project-1"
        setBusy={vi.fn()}
        setFlash={vi.fn()}
      />
    );
    await waitFor(() => expect(resolveFirst).not.toBeNull());
    rerender(
      <DelegatedCurationForm
        token="token-1"
        projectId="project-2"
        setBusy={vi.fn()}
        setFlash={vi.fn()}
      />
    );
    const select = await screen.findByLabelText("Delegated curation");
    await waitFor(() => expect(select).not.toBeDisabled());
    resolveFirst();
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(screen.getByLabelText("Delegated curation")).toHaveValue("off");
  });
});
