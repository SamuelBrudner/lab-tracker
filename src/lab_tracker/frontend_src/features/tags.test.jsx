import * as React from "react";

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { vi } from "vitest";

import { BenchTagsCard } from "./tags.jsx";
import { apiResponse, installFetchMock } from "../test/utils.js";

const PROJECTS = [
  { project_id: "p1", name: "Fly Circuits" },
  { project_id: "p2", name: "Wind Tunnel" },
];

const BENCH_TAG = {
  tag_binding_id: "tb-1",
  slug: "bench-3",
  label: "Bench 3 rig",
  project_id: "p1",
  session_id: null,
  hint: null,
  mode: null,
  log_breadcrumb: false,
  created_at: "2026-07-19T00:00:00Z",
  updated_at: "2026-07-19T00:00:00Z",
};

function renderCard(routes) {
  const fetchMock = installFetchMock([
    { match: "/projects?limit=200", response: apiResponse(PROJECTS) },
    { match: "/sessions?project_id=p1&limit=200", response: apiResponse([]) },
    ...routes,
  ]);
  const setFlash = vi.fn();
  render(<BenchTagsCard token="token-1" canWrite={true} setFlash={setFlash} />);
  return { fetchMock, setFlash };
}

it("lists the selected project's tags with their short paths", async () => {
  renderCard([
    {
      match: "/tags?project_id=p1&limit=200",
      response: apiResponse([BENCH_TAG]),
    },
  ]);

  expect(await screen.findByText("Bench 3 rig")).toBeInTheDocument();
  expect(screen.getByText("/t/bench-3")).toBeInTheDocument();
});

it("creates a tag and refreshes the list", async () => {
  const { fetchMock, setFlash } = renderCard([
    {
      match: "/tags?project_id=p1&limit=200",
      response: [apiResponse([]), apiResponse([BENCH_TAG])],
    },
    {
      match: "/tags",
      method: "POST",
      response: apiResponse(BENCH_TAG, 201),
    },
  ]);

  expect(
    await screen.findByText("No tags bound to this project yet.")
  ).toBeInTheDocument();

  fireEvent.change(screen.getByLabelText("Tag label"), {
    target: { value: "Bench 3 rig" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Create tag" }));

  expect(await screen.findByText("Bench 3 rig")).toBeInTheDocument();
  const createCall = fetchMock.mock.calls.find(
    ([url, init]) => url === "/tags" && init?.method === "POST"
  );
  expect(JSON.parse(createCall[1].body)).toEqual({
    label: "Bench 3 rig",
    project_id: "p1",
    log_breadcrumb: false,
  });
  expect(setFlash).toHaveBeenCalledWith('Tag "Bench 3 rig" bound to /t/bench-3.');
});

it("re-points a tag to another project via PATCH", async () => {
  const { fetchMock } = renderCard([
    {
      match: "/tags?project_id=p1&limit=200",
      response: [apiResponse([BENCH_TAG]), apiResponse([])],
    },
    {
      match: "/tags/bench-3",
      method: "PATCH",
      response: apiResponse({ ...BENCH_TAG, project_id: "p2" }),
    },
  ]);

  expect(await screen.findByText("Bench 3 rig")).toBeInTheDocument();

  fireEvent.change(screen.getByLabelText("Re-point to"), {
    target: { value: "p2" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Re-point" }));

  expect(
    await screen.findByText("No tags bound to this project yet.")
  ).toBeInTheDocument();
  const patchCall = fetchMock.mock.calls.find(
    ([url, init]) => url === "/tags/bench-3" && init?.method === "PATCH"
  );
  expect(JSON.parse(patchCall[1].body)).toEqual({ project_id: "p2" });
});

it("deletes a tag", async () => {
  const { fetchMock } = renderCard([
    {
      match: "/tags?project_id=p1&limit=200",
      response: [apiResponse([BENCH_TAG]), apiResponse([])],
    },
    {
      match: "/tags/bench-3",
      method: "DELETE",
      response: apiResponse(BENCH_TAG),
    },
  ]);

  expect(await screen.findByText("Bench 3 rig")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Delete" }));

  expect(
    await screen.findByText("No tags bound to this project yet.")
  ).toBeInTheDocument();
  expect(
    fetchMock.mock.calls.some(
      ([url, init]) => url === "/tags/bench-3" && init?.method === "DELETE"
    )
  ).toBe(true);
});

it("renders the print sheet with server QR SVGs and calls window.print", async () => {
  window.print = vi.fn();
  renderCard([
    {
      match: "/tags?project_id=p1&limit=200",
      response: apiResponse([BENCH_TAG]),
    },
    {
      match: "/tags/print?project_id=p1&limit=200",
      response: apiResponse([
        {
          slug: "bench-3",
          label: "Bench 3 rig",
          short_url: "https://lab.example.com/t/bench-3",
          qr_svg: '<svg viewBox="0 0 10 10"><rect width="10" height="10" /></svg>',
        },
      ]),
    },
  ]);

  // The print button stays disabled until the binding list is in.
  expect(await screen.findByText("Bench 3 rig")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Print sheet" }));

  expect(
    await screen.findByText("https://lab.example.com/t/bench-3")
  ).toBeInTheDocument();
  expect(document.querySelector(".tag-print-sheet svg")).not.toBeNull();
  await waitFor(() => expect(window.print).toHaveBeenCalledTimes(1));
});

const PRINT_ROUTE = {
  match: "/tags/print?project_id=p1&limit=200",
  response: apiResponse([
    {
      slug: "bench-3",
      label: "Bench 3 rig",
      short_url: "https://lab.example.com/t/bench-3",
      qr_svg: '<svg viewBox="0 0 10 10"><rect width="10" height="10" /></svg>',
    },
  ]),
};

it("invalidates the cached print sheet when a tag is deleted", async () => {
  window.print = vi.fn();
  renderCard([
    {
      match: "/tags?project_id=p1&limit=200",
      response: [apiResponse([BENCH_TAG]), apiResponse([])],
    },
    PRINT_ROUTE,
    {
      match: "/tags/bench-3",
      method: "DELETE",
      response: apiResponse(BENCH_TAG),
    },
  ]);

  expect(await screen.findByText("Bench 3 rig")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Print sheet" }));
  expect(
    await screen.findByText("https://lab.example.com/t/bench-3")
  ).toBeInTheDocument();

  fireEvent.click(screen.getByRole("button", { name: "Delete" }));

  await screen.findByText("No tags bound to this project yet.");
  // The sheet still listed the deleted tag; printing it would hand out a
  // dead QR, so it must be dropped.
  expect(screen.queryByText("https://lab.example.com/t/bench-3")).toBeNull();
});

it("invalidates the cached print sheet when a tag is created", async () => {
  window.print = vi.fn();
  const secondTag = { ...BENCH_TAG, tag_binding_id: "tb-2", slug: "bench-4", label: "Bench 4 rig" };
  renderCard([
    {
      match: "/tags?project_id=p1&limit=200",
      response: [apiResponse([BENCH_TAG]), apiResponse([BENCH_TAG, secondTag])],
    },
    PRINT_ROUTE,
    {
      match: "/tags",
      method: "POST",
      response: apiResponse(secondTag, 201),
    },
  ]);

  expect(await screen.findByText("Bench 3 rig")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Print sheet" }));
  expect(
    await screen.findByText("https://lab.example.com/t/bench-3")
  ).toBeInTheDocument();

  fireEvent.change(screen.getByLabelText("Tag label"), {
    target: { value: "Bench 4 rig" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Create tag" }));

  expect(await screen.findByText("Bench 4 rig")).toBeInTheDocument();
  // The sheet predates the new tag; it must be rebuilt before printing.
  expect(screen.queryByText("https://lab.example.com/t/bench-3")).toBeNull();
});

it("invalidates the cached print sheet when a tag is re-pointed", async () => {
  window.print = vi.fn();
  renderCard([
    {
      match: "/tags?project_id=p1&limit=200",
      response: [apiResponse([BENCH_TAG]), apiResponse([])],
    },
    PRINT_ROUTE,
    {
      match: "/tags/bench-3",
      method: "PATCH",
      response: apiResponse({ ...BENCH_TAG, project_id: "p2" }),
    },
  ]);

  expect(await screen.findByText("Bench 3 rig")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Print sheet" }));
  expect(
    await screen.findByText("https://lab.example.com/t/bench-3")
  ).toBeInTheDocument();

  fireEvent.change(screen.getByLabelText("Re-point to"), {
    target: { value: "p2" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Re-point" }));

  await screen.findByText("No tags bound to this project yet.");
  expect(screen.queryByText("https://lab.example.com/t/bench-3")).toBeNull();
});
