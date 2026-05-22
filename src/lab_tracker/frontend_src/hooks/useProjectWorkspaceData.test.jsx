import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useProjectWorkspaceData } from "./useProjectWorkspaceData.js";
import { apiResponse, installFetchMock } from "../test/utils.js";

const STORAGE_KEY = "lab-tracker:last-used-project-id";

const PROJECT_A = { project_id: "proj-a", name: "Project A" };
const PROJECT_B = { project_id: "proj-b", name: "Project B" };

const ZERO_PAGE_META = { total: 0, limit: 1, offset: 0 };

function projectsListResponse(projects) {
  return apiResponse(projects, 200, { total: projects.length, limit: 200, offset: 0 });
}

function emptyCountResponse() {
  return apiResponse([], 200, ZERO_PAGE_META);
}

function renderWorkspaceHook() {
  // setBusy/setFlash are dependencies of the workspace effect; stabilizing
  // them prevents the effect from re-firing on every render.
  const setBusy = vi.fn();
  const setFlash = vi.fn();
  const view = renderHook(() =>
    useProjectWorkspaceData({
      token: "test-token",
      setBusy,
      setFlash,
    })
  );
  return { ...view, setBusy, setFlash };
}

beforeEach(() => {
  localStorage.clear();
});

afterEach(() => {
  vi.restoreAllMocks();
  localStorage.clear();
});

describe("useProjectWorkspaceData last-used project persistence", () => {
  it("restores the stored project on mount when it is still available", async () => {
    localStorage.setItem(STORAGE_KEY, PROJECT_B.project_id);

    installFetchMock([
      {
        method: "GET",
        match: /\/projects\?/,
        response: projectsListResponse([PROJECT_A, PROJECT_B]),
      },
      {
        method: "GET",
        match: new RegExp(`/questions\\?project_id=${PROJECT_B.project_id}`),
        response: emptyCountResponse(),
      },
      {
        method: "GET",
        match: new RegExp(`/datasets\\?project_id=${PROJECT_B.project_id}`),
        response: emptyCountResponse(),
      },
      {
        method: "GET",
        match: new RegExp(`/notes\\?project_id=${PROJECT_B.project_id}`),
        response: emptyCountResponse(),
      },
    ]);

    const { result } = renderWorkspaceHook();

    await waitFor(() => {
      expect(result.current.projects).toHaveLength(2);
    });
    expect(result.current.selectedProjectId).toBe(PROJECT_B.project_id);
  });

  it("falls back to the first project and overwrites a stale stored id", async () => {
    localStorage.setItem(STORAGE_KEY, "missing-project");

    installFetchMock([
      {
        method: "GET",
        match: /\/projects\?/,
        response: projectsListResponse([PROJECT_A, PROJECT_B]),
      },
      {
        method: "GET",
        match: new RegExp(`/questions\\?project_id=${PROJECT_A.project_id}`),
        response: emptyCountResponse(),
      },
      {
        method: "GET",
        match: new RegExp(`/datasets\\?project_id=${PROJECT_A.project_id}`),
        response: emptyCountResponse(),
      },
      {
        method: "GET",
        match: new RegExp(`/notes\\?project_id=${PROJECT_A.project_id}`),
        response: emptyCountResponse(),
      },
    ]);

    const { result } = renderWorkspaceHook();

    await waitFor(() => {
      expect(result.current.selectedProjectId).toBe(PROJECT_A.project_id);
    });
    expect(localStorage.getItem(STORAGE_KEY)).toBe(PROJECT_A.project_id);
  });

  it("persists the user's selection on change", async () => {
    installFetchMock([
      {
        method: "GET",
        match: /\/projects\?/,
        response: projectsListResponse([PROJECT_A, PROJECT_B]),
      },
      {
        method: "GET",
        match: /\/(questions|datasets|notes)\?project_id=/,
        response: emptyCountResponse(),
      },
    ]);

    const { result } = renderWorkspaceHook();

    await waitFor(() => {
      expect(result.current.selectedProjectId).toBe(PROJECT_A.project_id);
    });

    act(() => {
      result.current.setSelectedProjectId(PROJECT_B.project_id);
    });

    await waitFor(() => {
      expect(localStorage.getItem(STORAGE_KEY)).toBe(PROJECT_B.project_id);
    });
  });
});
