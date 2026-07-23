import * as React from "react";
import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { EnrollPage } from "./enroll.jsx";
import { createAuthStorage, persistAuthSession } from "../shared/auth-storage.js";
import { TOKEN_STORAGE_KEY } from "../shared/constants.js";
import { auth as authGateway } from "../shared/gateways/index.js";

vi.mock("../shared/gateways/index.js", () => ({
  auth: {
    consumeDeviceEnrollment: vi.fn(),
  },
}));

describe("EnrollPage credential persistence", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    window.history.replaceState({}, "", "/app/enroll?offer=pairing-offer");
  });

  it("does not report success or reload when a stale token cannot be replaced", async () => {
    authGateway.consumeDeviceEnrollment.mockResolvedValue({
      created_at: "2026-07-22T12:00:00Z",
      device_token_id: "device-1",
      label: "Phone",
      secret: "new-device-token",
    });
    const backing = new Map([[TOKEN_STORAGE_KEY, "stale-token"]]);
    const backingStore = {
      getItem: (key) => backing.get(key) ?? null,
      setItem: () => {
        throw new Error("QuotaExceededError");
      },
      removeItem: (key) => backing.delete(key),
    };
    const storage = createAuthStorage(backingStore);
    expect(storage.getItem(TOKEN_STORAGE_KEY)).toBe("stale-token");

    const reload = vi.fn();
    const replace = vi.fn();
    const setFlash = vi.fn();
    render(
      <EnrollPage
        persistTokenForReload={(token) => persistAuthSession(storage, token)}
        reload={reload}
        replace={replace}
        setFlash={setFlash}
      />
    );

    expect(
      await screen.findByText(/this browser couldn’t save the new credential/i)
    ).toBeInTheDocument();
    await waitFor(() => expect(authGateway.consumeDeviceEnrollment).toHaveBeenCalledOnce());
    expect(backing.has(TOKEN_STORAGE_KEY)).toBe(false);
    expect(storage.getItem(TOKEN_STORAGE_KEY)).toBe("");
    expect(createAuthStorage(backingStore).getItem(TOKEN_STORAGE_KEY)).toBe("");
    expect(setFlash).not.toHaveBeenCalled();
    expect(replace).not.toHaveBeenCalled();
    expect(reload).not.toHaveBeenCalled();
  });
});
