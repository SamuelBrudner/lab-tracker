import { describe, expect, it } from "vitest";

import { REJECT_REASONS, reasonForKey } from "./review-reasons.js";

describe("review reasons", () => {
  it("maps digits 1-7 to seven distinct server reject reasons", () => {
    expect(REJECT_REASONS.map((reason) => reason.key)).toEqual(["1", "2", "3", "4", "5", "6", "7"]);
    expect(new Set(REJECT_REASONS.map((reason) => reason.value)).size).toBe(7);
    expect(REJECT_REASONS.map((reason) => reason.value)).toEqual([
      "duplicate_of_existing",
      "wrong_target",
      "unsupported_by_source",
      "already_captured",
      "not_relevant",
      "not_now",
      "other",
    ]);
    expect(reasonForKey(REJECT_REASONS, "3").value).toBe("unsupported_by_source");
    expect(reasonForKey(REJECT_REASONS, "8")).toBeNull();
  });
});
