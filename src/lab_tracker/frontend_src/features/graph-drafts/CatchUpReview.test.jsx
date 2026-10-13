import * as React from "react";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import { apiResponse, installFetchMock } from "../../test/utils.js";
import { CatchUpReview } from "./CatchUpReview.jsx";

it("combines pending reviews and prioritizes a consequential decision over routine links", async () => {
  const makeReview = (id, lane, question) => ({
    change_set_id: id, project_id: "project", status: "ready", created_at: "2026-10-13T12:00:00Z", context_packet: {}, draft_mode: "graph_batch",
    operations: [{ operation_id: id, entity_type: "question", op: "create", payload: { text: question }, status: "proposed" }],
    triage: { summary: { judgment: lane === "judgment" ? 1 : 0, confirmations: lane === "confirmations" ? 1 : 0, later: 0 }, groups: [{ kind: "operations", group_id: id, lane, question, operation_ids: [id], pending_operation_ids: [id], deferred_operation_ids: [], resolved: false }] },
  });
  const link = makeReview("link", "confirmations", "Confirm the capture links?");
  const claim = makeReview("claim", "judgment", "Does the evidence change our interpretation?");
  installFetchMock([
    { match: "/batches?limit=5&mine=true&offset=0", response: apiResponse([link, claim]) },
    { match: "/graph-drafts/link?record_view=false", response: apiResponse(link) },
    { match: "/graph-drafts/claim?record_view=false", response: apiResponse(claim) },
    { match: "/graph-drafts/claim", response: apiResponse(claim) },
  ]);
  render(<CatchUpReview token="token" user={{ role: "admin", user_id: "user" }} onClose={vi.fn()} navigate={vi.fn()} setBusy={vi.fn()} setFlash={vi.fn()} />);
  expect(await screen.findByText("2 unresolved decisions across 2 loaded reviews.")).toBeInTheDocument();
  expect(await screen.findByRole("heading", { name: "Does the evidence change our interpretation?" })).toBeInTheDocument();
  expect(screen.getByLabelText("Choose a pending decision")).toHaveValue("claim:claim");
});

it("ignores the previous account's late response without fetching its reviews", async () => {
  let finishOldPage;
  const oldPage = new Promise((resolve) => { finishOldPage = resolve; });
  const fetchMock = installFetchMock([
    {
      match: "/batches?limit=5&mine=true&offset=0",
      response: ({ init }) => init.headers.Authorization === "Bearer old"
        ? oldPage
        : apiResponse([]),
    },
  ]);
  const props = { onClose: vi.fn(), navigate: vi.fn(), setBusy: vi.fn(), setFlash: vi.fn() };
  const { rerender } = render(<CatchUpReview {...props} token="old" />);
  await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
  rerender(<CatchUpReview {...props} token="new" />);
  expect(await screen.findByText("0 unresolved decisions across 0 loaded reviews.")).toBeInTheDocument();
  await act(async () => { finishOldPage(apiResponse([{ change_set_id: "old-review" }])); });
  expect(screen.getByRole("status")).toHaveTextContent("No unresolved decisions in the loaded reviews.");
  expect(screen.queryByRole("button", { name: "Include more reviews" })).not.toBeInTheDocument();
  expect(fetchMock).toHaveBeenCalledTimes(2);
});

it("keeps the final submission or commit step after the last verdict", async () => {
  let draft = {
    change_set_id: "draft", project_id: "project", status: "ready", draft_mode: "graph_batch",
    context_packet: {}, created_at: "2026-10-13T12:00:00Z",
    operations: [{ operation_id: "question", entity_type: "question", op: "create", status: "proposed", payload: { text: "The next research question" } }],
    triage: { summary: { judgment: 1, confirmations: 0, later: 0 }, groups: [{ kind: "operations", group_id: "question", revision: "current", lane: "judgment", question: "Keep this question?", operation_ids: ["question"], pending_operation_ids: ["question"], deferred_operation_ids: [], resolved: false }] },
  };
  installFetchMock([
    { match: "/batches?limit=5&mine=true&offset=0", response: apiResponse([draft]) },
    { match: /^\/graph-drafts\/draft(?:\?record_view=false)?$/, response: () => apiResponse(draft) },
    { match: "/graph-drafts/draft/triage-decision", method: "POST", response: () => {
      draft = structuredClone(draft);
      draft.operations[0].status = "accepted";
      draft.triage.groups[0].resolved = true;
      draft.triage.summary.judgment = 0;
      return apiResponse(draft);
    } },
    { match: "/graph-drafts/draft/commit", method: "POST", response: () => {
      draft = { ...draft, status: "committed" };
      return apiResponse(draft);
    } },
  ]);
  render(<CatchUpReview token="token" user={{ role: "admin", user_id: "user" }} onClose={vi.fn()} navigate={vi.fn()} setBusy={vi.fn()} setFlash={vi.fn()} />);
  fireEvent.click(await screen.findByRole("button", { name: "Keep this decision" }));
  await waitFor(() => expect(screen.getByLabelText("Choose a pending decision")).toHaveValue("draft:finish"));
  const finish = await screen.findByRole("button", { name: "Finish review and commit kept changes" });
  await waitFor(() => expect(finish).toBeEnabled());
  fireEvent.click(finish);
  expect(await screen.findByText("No unresolved decisions in the loaded reviews.")).toBeInTheDocument();
  expect(screen.queryByLabelText("Choose a pending decision")).not.toBeInTheDocument();
});
