import { describe, expect, it } from "vitest";

import { starterContextKeys } from "./onboarding-context.js";

describe("starterContextKeys", () => {
  it("creates stable, opaque retry keys scoped to project, user, and context", async () => {
    const input = {
      context: "Aim 1: test an unpublished mechanism.",
      projectId: "project-1",
      userId: "user-1",
    };

    const first = await starterContextKeys(input);
    const retry = await starterContextKeys(input);
    const otherProject = await starterContextKeys({
      ...input,
      projectId: "project-2",
    });

    expect(first).toEqual(retry);
    expect(first.clientCaptureId).toMatch(
      /^onboarding-context-[0-9a-f]{64}$/
    );
    expect(first.idempotencyKey).toMatch(
      /^starter-questions:[0-9a-f]{64}$/
    );
    expect(first.clientCaptureId).not.toContain(input.context);
    expect(first.idempotencyKey).not.toContain(input.context);
    expect(otherProject).not.toEqual(first);
  });
});
