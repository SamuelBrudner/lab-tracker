import { describe, expect, it } from "vitest";

import { matchingClientSetup, normalizeSourceRevision } from "./client-setup.js";

const REVISION = "0123456789abcdef0123456789abcdef01234567";

describe("matchingClientSetup", () => {
  it("builds every install and verification command from one immutable revision", () => {
    const setup = matchingClientSetup(REVISION);

    expect(setup).not.toBeNull();
    expect(setup.revision).toBe(REVISION);
    expect(setup.toolInstallCommand).toContain(`@${REVISION}`);
    expect(setup.projectInstallCommand).toContain(`@${REVISION}`);
    expect(setup.verifyClientCommand).toContain(
      `--expected-revision ${REVISION}`
    );
    expect(setup.verifyMcpCommand).toContain(
      `--expected-revision ${REVISION}`
    );
    expect(setup.projectImportCommand).toContain("import lab_tracker_client");
    expect(JSON.stringify(setup)).not.toContain("@main");
  });

  it.each([
    "",
    "unknown",
    "main",
    "0123456",
    `${REVISION}extra`,
  ])("fails closed for an unpinned source revision: %s", (revision) => {
    expect(normalizeSourceRevision(revision)).toBe("");
    expect(matchingClientSetup(revision)).toBeNull();
  });
});
