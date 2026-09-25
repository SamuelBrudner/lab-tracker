import { describe, expect, it } from "vitest";

import {
  contextOptions,
  decisionCounts,
  decisionFlashMessage,
  defaultCommitMessage,
  editableStringFields,
  nextPayloadWithTarget,
  operationProposalText,
  payloadTargetId,
  payloadText,
  sourceRegionStyle,
  sourceRegions,
  spokenReviewScript,
  statusClass,
} from "./format.js";

describe("statusClass", () => {
  it("maps terminal-positive statuses to approved", () => {
    expect(statusClass("accepted")).toBe("pill review-approved");
    expect(statusClass("committed")).toBe("pill review-approved");
  });
  it("maps rejected/failed to rejected and everything else to pending", () => {
    expect(statusClass("rejected")).toBe("pill review-rejected");
    expect(statusClass("proposed")).toBe("pill review-pending");
  });
});

describe("sourceRegionStyle", () => {
  it("scales fractional coordinates to percentages", () => {
    expect(sourceRegionStyle({ x: 0.1, y: 0.2, width: 0.3, height: 0.4 })).toEqual({
      left: "10%",
      top: "20%",
      width: "30%",
      height: "40%",
    });
  });

  it("rejects negative, non-numeric, or out-of-range regions", () => {
    expect(sourceRegionStyle({ x: -1, y: 0, width: 1, height: 1 })).toBeNull();
    expect(sourceRegionStyle({ x: "a", y: 0, width: 1, height: 1 })).toBeNull();
    expect(sourceRegionStyle(null)).toBeNull();
    expect(sourceRegionStyle({ x: 200, y: 0, width: 10, height: 10 })).toBeNull();
  });

  it("clamps a box that would overflow the image", () => {
    const style = sourceRegionStyle({ x: 90, y: 90, width: 50, height: 50 });
    expect(style).toEqual({ left: "90%", top: "90%", width: "10%", height: "10%" });
  });
});

describe("sourceRegions", () => {
  it("collects only refs with valid regions, tagged by operation", () => {
    const changeSet = {
      operations: [
        {
          operation_id: "op-1",
          source_refs: [
            { region: { x: 0, y: 0, width: 0.5, height: 0.5 }, label: "a" },
            { region: { x: -1, y: 0, width: 1, height: 1 } },
          ],
        },
      ],
    };
    const regions = sourceRegions(changeSet);
    expect(regions).toHaveLength(1);
    expect(regions[0].operation.operation_id).toBe("op-1");
  });
});

describe("payloadText", () => {
  it("pretty-prints each operation payload by id", () => {
    const entries = payloadText({
      operations: [{ operation_id: "op-1", payload: { text: "hi" } }],
    });
    expect(entries["op-1"]).toBe(JSON.stringify({ text: "hi" }, null, 2));
  });
});

describe("payload target helpers", () => {
  it("reads a target id for an entity type", () => {
    const payload = { targets: [{ entity_type: "question", entity_id: "q-1" }] };
    expect(payloadTargetId(payload, "question")).toBe("q-1");
    expect(payloadTargetId(payload, "dataset")).toBe("");
  });

  it("replaces a target of the same type immutably", () => {
    const payload = { text: "x", targets: [{ entity_type: "question", entity_id: "q-1" }] };
    const next = nextPayloadWithTarget(payload, "question", "q-2");
    expect(next.targets).toEqual([{ entity_type: "question", entity_id: "q-2" }]);
    expect(payload.targets[0].entity_id).toBe("q-1"); // original untouched
  });

  it("drops the target when the id is empty", () => {
    const payload = { targets: [{ entity_type: "question", entity_id: "q-1" }] };
    expect(nextPayloadWithTarget(payload, "question", "").targets).toEqual([]);
  });
});

describe("contextOptions", () => {
  it("reads direct context options and falls back to batch projects", () => {
    expect(
      contextOptions({ context_packet: { active_or_staged_questions: [{ id: "q-1" }] } }, "question")
    ).toEqual([{ id: "q-1" }]);
    expect(
      contextOptions(
        { context_packet: { projects: [{ recent_datasets: [{ id: "d-1" }] }] } },
        "dataset"
      )
    ).toEqual([{ id: "d-1" }]);
  });
});

describe("operationProposalText", () => {
  it("prefers an in-progress edited payload over the server payload", () => {
    const operation = { operation_id: "op-1", payload: { text: "old" } };
    const edited = { "op-1": JSON.stringify({ text: "new" }) };
    expect(operationProposalText(operation, edited)).toBe("new");
  });

  it("falls back to the server payload when the edit is invalid JSON", () => {
    const operation = { operation_id: "op-1", payload: { text: "old" } };
    expect(operationProposalText(operation, { "op-1": "{not json" })).toBe("old");
  });
});

describe("spokenReviewScript", () => {
  it("returns empty for no change set", () => {
    expect(spokenReviewScript(null)).toBe("");
  });

  it("narrates summary, proposal count, and uncertainties", () => {
    const script = spokenReviewScript({
      summary: "A review",
      operations: [{ operation_id: "op-1", semantic_type: "create_note", payload: { text: "hi" } }],
      clarification_requests: ["What rig?"],
    });
    expect(script).toContain("Review summary. A review");
    expect(script).toContain("There is 1 proposal.");
    expect(script).toContain("Questions for you. What rig?");
  });
});

describe("decisionFlashMessage", () => {
  const operation = {
    op: "create",
    entity_type: "claim",
    payload: { statement: "Sleep deprivation reduces courtship" },
    status: "proposed",
  };

  it("names the decision and the proposal it applied to", () => {
    expect(decisionFlashMessage(operation, "accepted")).toBe(
      "Accepted: Sleep deprivation reduces courtship"
    );
    expect(decisionFlashMessage(operation, "rejected")).toBe(
      "Rejected: Sleep deprivation reduces courtship"
    );
    expect(decisionFlashMessage(operation, "proposed")).toBe(
      "Deferred: Sleep deprivation reduces courtship"
    );
  });

  it("reports an edit-only save without inventing a decision", () => {
    expect(decisionFlashMessage(operation, undefined)).toBe(
      "Saved edits to: Sleep deprivation reduces courtship"
    );
  });

  it("clips a long proposal so the confirmation stays one line", () => {
    const long = { ...operation, payload: { statement: "x".repeat(120) } };
    expect(decisionFlashMessage(long, "accepted")).toBe(`Accepted: ${"x".repeat(77)}...`);
  });
});

describe("decisionCounts", () => {
  it("tallies kept, rejected, and undecided proposals", () => {
    expect(
      decisionCounts({
        operations: [
          { status: "accepted" },
          { status: "accepted" },
          { status: "rejected" },
          { status: "proposed" },
          { status: "applied" },
        ],
      })
    ).toEqual({ accepted: 2, proposed: 1, rejected: 1, other: 1 });
    expect(decisionCounts(null)).toEqual({ accepted: 0, proposed: 0, rejected: 0, other: 0 });
  });
});

describe("defaultCommitMessage", () => {
  it("describes a daily review by date and how much was kept", () => {
    expect(
      defaultCommitMessage(
        {
          created_at: "2026-07-15T20:00:00Z",
          draft_mode: "graph_batch",
          operations: [{}, {}, {}],
        },
        2
      )
    ).toBe("Daily review 2026-07-15: kept 2 of 3 proposals");
  });

  it("labels a single-capture draft differently and copes with no date", () => {
    expect(
      defaultCommitMessage({ draft_mode: "graph_context", operations: [{}] }, 1)
    ).toBe("Capture review: kept 1 of 1 proposals");
    expect(defaultCommitMessage(null, 0)).toBe("");
  });
});

describe("editableStringFields", () => {
  it("offers typed editors for free-text fields only", () => {
    expect(
      editableStringFields({
        statement: "Sleep reduces courtship",
        falsification_criteria: "No drop in courtship index after deprivation",
        text: "already typed",
        raw_content: "already typed",
        primary_question_id: "q-1",
        dataset_ids: "d-1",
        entity_type: "claim",
        claim_type: "empirical",
        confidence: 0.4,
        label: "short",
      })
    ).toEqual([
      { key: "statement", label: "Statement", multiline: true },
      {
        key: "falsification_criteria",
        label: "Falsification criteria",
        multiline: true,
      },
      { key: "label", label: "Label", multiline: false },
    ]);
  });

  it("treats a long unknown string as multi-line", () => {
    expect(editableStringFields({ caption: "c".repeat(81) })).toEqual([
      { key: "caption", label: "Caption", multiline: true },
    ]);
    expect(editableStringFields(null)).toEqual([]);
  });
});
