import { act, renderHook, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { apiResponse, installFetchMock } from "../test/utils.js";
import { useMemberOnboarding } from "./useMemberOnboarding.js";

function orientation(projectId, state = "not_started") {
  return {
    alignment: null,
    brief_markdown: "",
    capabilities: {
      can_align: true,
      can_capture: true,
      can_commit: false,
      can_create_checkpoint: true,
      can_read: true,
    },
    checkpoint: null,
    first_capture: null,
    guided_fields: null,
    map_items: [],
    member_complete: state === "complete",
    owner_commit_pending: false,
    project_id: projectId,
    role: "contributor",
    state,
  };
}

function renderOnboardingHook(initialProps) {
  const setBusy = vi.fn();
  const setFlash = vi.fn();
  const view = renderHook(
    ({ projectId, token }) =>
      useMemberOnboarding({ projectId, token, setBusy, setFlash }),
    { initialProps }
  );
  return { ...view, setBusy, setFlash };
}

describe("useMemberOnboarding request context", () => {
  it("clears project A immediately and ignores it after a switch to project B", async () => {
    let resolveB;
    const deferredB = new Promise((resolve) => {
      resolveB = resolve;
    });
    installFetchMock([
      {
        match: "/projects/project-a/member-onboarding",
        response: apiResponse(orientation("project-a", "checkpoint_ready")),
      },
      {
        match: "/projects/project-b/member-onboarding",
        response: () => deferredB,
      },
    ]);
    const { result, rerender } = renderOnboardingHook({
      projectId: "project-a",
      token: "token-1",
    });

    await waitFor(() => expect(result.current.onboarding?.project_id).toBe("project-a"));
    rerender({ projectId: "project-b", token: "token-1" });

    expect(result.current.onboarding).toBeNull();
    expect(result.current.loading).toBe(true);

    await act(async () => {
      resolveB(apiResponse(orientation("project-b", "capture_pending")));
      await deferredB;
    });
    await waitFor(() => expect(result.current.onboarding?.project_id).toBe("project-b"));
  });

  it("clears data when the session token changes", async () => {
    let resolveSecond;
    const deferredSecond = new Promise((resolve) => {
      resolveSecond = resolve;
    });
    installFetchMock([
      {
        match: "/projects/project-a/member-onboarding",
        response: [
          apiResponse(orientation("project-a", "checkpoint_ready")),
          deferredSecond,
        ],
      },
    ]);
    const { result, rerender } = renderOnboardingHook({
      projectId: "project-a",
      token: "token-1",
    });

    await waitFor(() => expect(result.current.onboarding?.state).toBe("checkpoint_ready"));
    rerender({ projectId: "project-a", token: "token-2" });

    expect(result.current.onboarding).toBeNull();
    expect(result.current.loading).toBe(true);

    await act(async () => {
      resolveSecond(apiResponse(orientation("project-a", "capture_pending")));
      await deferredSecond;
    });
    await waitFor(() => expect(result.current.onboarding?.state).toBe("capture_pending"));
  });

  it("drops an older poll response that finishes after a newer poll", async () => {
    let resolveSlow;
    let resolveFast;
    const slowResponse = new Promise((resolve) => {
      resolveSlow = resolve;
    });
    const fastResponse = new Promise((resolve) => {
      resolveFast = resolve;
    });
    installFetchMock([
      {
        match: "/projects/project-a/member-onboarding",
        response: [
          apiResponse(orientation("project-a", "checkpoint_ready")),
          slowResponse,
          fastResponse,
        ],
      },
    ]);
    const { result } = renderOnboardingHook({
      projectId: "project-a",
      token: "token-1",
    });
    await waitFor(() => expect(result.current.onboarding?.state).toBe("checkpoint_ready"));

    let slowLoad;
    let fastLoad;
    act(() => {
      slowLoad = result.current.load();
      fastLoad = result.current.load();
    });
    await act(async () => {
      resolveFast(apiResponse(orientation("project-a", "complete")));
      await fastLoad;
    });
    expect(result.current.onboarding?.state).toBe("complete");

    await act(async () => {
      resolveSlow(apiResponse(orientation("project-a", "checkpoint_ready")));
      await slowLoad;
    });
    expect(result.current.onboarding?.state).toBe("complete");
  });
});
