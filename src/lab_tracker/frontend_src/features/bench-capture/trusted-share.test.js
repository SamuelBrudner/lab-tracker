import { describe, expect, it, vi } from "vitest";

import { claimLocalDraftsFor, clearAllLocalDrafts } from "../../hooks/useLocalDraft.js";
import {
  SHARE_TRUST_KEY,
  formatTrustRemaining,
  grantShareTrust,
  readShareTrust,
  revokeShareTrust,
} from "./trusted-share.js";

const HOUR = 60 * 60 * 1000;
const T0 = Date.parse("2026-09-28T10:00:00Z");

function grant(overrides = {}) {
  return grantShareTrust({
    hours: 2,
    now: T0,
    ownerId: "owner-1",
    projectId: "project-1",
    sessionId: "session-1",
    sessionLabel: "Operational session",
    ...overrides,
  });
}

describe("trusted share window", () => {
  it("is granted for one project and session and read back until it expires", () => {
    expect(grant()).toMatchObject({ expiresAt: T0 + 2 * HOUR, sessionId: "session-1" });

    expect(
      readShareTrust({ ownerId: "owner-1", projectId: "project-1", now: T0 + HOUR })
    ).toMatchObject({ projectId: "project-1", sessionId: "session-1" });
    expect(
      readShareTrust({ ownerId: "owner-1", projectId: "project-1", now: T0 + 2 * HOUR })
    ).toBeNull();
    // Expired windows are removed, not just ignored.
    expect(localStorage.getItem(SHARE_TRUST_KEY)).toBeNull();
  });

  it("only offers 1, 2 or 4 hours and needs an owner, project and session", () => {
    expect(grant({ hours: 8 })).toBeNull();
    expect(grant({ hours: 0.5 })).toBeNull();
    expect(grant({ ownerId: "" })).toBeNull();
    expect(grant({ sessionId: "" })).toBeNull();
    expect(localStorage.getItem(SHARE_TRUST_KEY)).toBeNull();
    expect(grant({ hours: 4 })?.expiresAt).toBe(T0 + 4 * HOUR);
  });

  it("never applies to another project", () => {
    grant();

    expect(readShareTrust({ ownerId: "owner-1", projectId: "project-2", now: T0 })).toBeNull();
    // The window stays for its own project.
    expect(readShareTrust({ ownerId: "owner-1", projectId: "project-1", now: T0 })).not.toBeNull();
  });

  it("is dropped for anyone but the person who opened it", () => {
    grant();

    expect(readShareTrust({ ownerId: "owner-2", projectId: "project-1", now: T0 })).toBeNull();
    expect(readShareTrust({ ownerId: "owner-1", projectId: "project-1", now: T0 })).toBeNull();
  });

  it("ends at sign-out and when someone else signs in on this browser", () => {
    grant();
    clearAllLocalDrafts();
    expect(readShareTrust({ ownerId: "owner-1", projectId: "project-1", now: T0 })).toBeNull();

    claimLocalDraftsFor("owner-1");
    grant();
    claimLocalDraftsFor("owner-2");
    expect(readShareTrust({ ownerId: "owner-1", projectId: "project-1", now: T0 })).toBeNull();
  });

  it("can be stopped", () => {
    grant();
    revokeShareTrust();
    expect(readShareTrust({ ownerId: "owner-1", projectId: "project-1", now: T0 })).toBeNull();
  });

  it("rejects tampered or implausible records", () => {
    localStorage.setItem(SHARE_TRUST_KEY, "{not json");
    expect(readShareTrust({ ownerId: "owner-1", projectId: "project-1", now: T0 })).toBeNull();

    localStorage.setItem(
      SHARE_TRUST_KEY,
      JSON.stringify({
        expiresAt: T0 + 48 * HOUR,
        grantedAt: T0,
        ownerId: "owner-1",
        projectId: "project-1",
        sessionId: "session-1",
      })
    );
    expect(readShareTrust({ ownerId: "owner-1", projectId: "project-1", now: T0 })).toBeNull();
    expect(localStorage.getItem(SHARE_TRUST_KEY)).toBeNull();
  });

  it("trusts nothing when storage refuses reads or writes", () => {
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("QuotaExceededError");
    });
    expect(grant()).toBeNull();
    vi.restoreAllMocks();

    grant();
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("SecurityError");
    });
    expect(readShareTrust({ ownerId: "owner-1", projectId: "project-1", now: T0 })).toBeNull();
    expect(() => revokeShareTrust()).not.toThrow();
  });

  it("formats the time left", () => {
    expect(formatTrustRemaining(2 * HOUR)).toBe("2 h");
    expect(formatTrustRemaining(HOUR + 5 * 60 * 1000)).toBe("1 h 5 min");
    expect(formatTrustRemaining(12 * 60 * 1000 + 30 * 1000)).toBe("12 min");
    expect(formatTrustRemaining(20 * 1000)).toBe("under a minute");
    expect(formatTrustRemaining(-5)).toBe("under a minute");
  });
});
