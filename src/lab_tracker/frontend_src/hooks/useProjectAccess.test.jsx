import * as React from "react";

import { render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { apiResponse, installFetchMock } from "../test/utils.js";
import { useProjectAccess } from "./useProjectAccess.js";

const USER = { user_id: "user-1", role: "editor" };

function AccessHarness({ projectId, user = USER }) {
  const access = useProjectAccess(projectId, { token: "t", user, enabled: true });
  return (
    <>
      <span data-testid="status">{access.status}</span>
      <span data-testid="role">{access.role}</span>
      <span data-testid="canContribute">{String(access.canContribute)}</span>
      <span data-testid="canManage">{String(access.canManage)}</span>
    </>
  );
}

describe("useProjectAccess", () => {
  it("ignores a late members response for a previously-selected project", async () => {
    let resolveA;
    const deferredA = new Promise((resolve) => {
      resolveA = resolve;
    });
    installFetchMock([
      { match: /\/projects\/proj-a\/members/, response: () => deferredA },
      {
        match: /\/projects\/proj-b\/members/,
        response: apiResponse([{ user_id: "user-1", role: "viewer" }]),
      },
    ]);

    const { rerender } = render(<AccessHarness projectId="proj-a" />);
    // Select B while A's members response is still in flight.
    rerender(<AccessHarness projectId="proj-b" />);

    await waitFor(() => expect(screen.getByTestId("role")).toHaveTextContent("viewer"));
    expect(screen.getByTestId("canContribute")).toHaveTextContent("false");

    // A's late response (which would grant owner) must NOT change B's access.
    resolveA(apiResponse([{ user_id: "user-1", role: "owner" }]));
    await Promise.resolve();
    await Promise.resolve();

    expect(screen.getByTestId("role")).toHaveTextContent("viewer");
    expect(screen.getByTestId("canContribute")).toHaveTextContent("false");
  });

  it("denies while access is unknown, then grants once membership resolves", async () => {
    let resolve;
    const deferred = new Promise((r) => {
      resolve = r;
    });
    installFetchMock([{ match: /\/projects\/proj-a\/members/, response: () => deferred }]);

    render(<AccessHarness projectId="proj-a" />);

    await waitFor(() => expect(screen.getByTestId("status")).toHaveTextContent("loading"));
    expect(screen.getByTestId("canContribute")).toHaveTextContent("false");

    resolve(apiResponse([{ user_id: "user-1", role: "contributor" }]));
    await waitFor(() => expect(screen.getByTestId("canContribute")).toHaveTextContent("true"));
    expect(screen.getByTestId("canManage")).toHaveTextContent("false");
  });

  it("grants a global admin owner-equivalent access without waiting on the fetch", () => {
    installFetchMock([{ match: /\/projects\/proj-a\/members/, response: apiResponse([]) }]);
    render(<AccessHarness projectId="proj-a" user={{ user_id: "admin-1", role: "admin" }} />);
    expect(screen.getByTestId("canContribute")).toHaveTextContent("true");
    expect(screen.getByTestId("canManage")).toHaveTextContent("true");
  });
});
