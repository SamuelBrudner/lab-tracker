import { describe, expect, it, vi } from "vitest";

import {
  AUTH_REJECTED_EVENT,
  NetworkError,
  apiListRequest,
  apiRequest,
  fetchAllPages,
  fetchProtectedBlobResource,
} from "./api.js";
import { ContractError } from "./contract.js";
import {
  apiResponse,
  binaryResponse,
  errorResponse,
  installFetchMock,
} from "../test/utils.js";

describe("network failures", () => {
  it("wraps a rejected fetch in NetworkError, keeping its message and cause", async () => {
    const offline = new TypeError("Failed to fetch");
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw offline;
      })
    );

    const error = await apiRequest("/resource").catch((caught) => caught);

    expect(error).toBeInstanceOf(NetworkError);
    expect(error.message).toBe("Failed to fetch");
    expect(error.cause).toBe(offline);
    expect(error.status).toBeUndefined();
  });

  it("does not report a malformed successful response as a network failure", async () => {
    installFetchMock([
      {
        match: "/resource",
        response: new Response("ok", { headers: { "content-type": "text/plain" }, status: 200 }),
      },
    ]);

    const error = await apiRequest("/resource").catch((caught) => caught);

    expect(error).toBeInstanceOf(ContractError);
    expect(error).not.toBeInstanceOf(NetworkError);
  });
});

describe("strict JSON envelope helpers", () => {
  it("apiRequest rejects malformed successful resource envelopes", async () => {
    installFetchMock([
      {
        match: "/resource",
        response: new Response(JSON.stringify({ result: { id: "x" } }), {
          headers: { "content-type": "application/json" },
          status: 200,
        }),
      },
    ]);

    await expect(apiRequest("/resource")).rejects.toBeInstanceOf(ContractError);
  });

  it("apiRequest rejects a null resource from a successful response", async () => {
    installFetchMock([
      { match: "/resource", response: apiResponse(null, 200) },
    ]);

    await expect(apiRequest("/resource")).rejects.toBeInstanceOf(ContractError);
  });

  it("apiListRequest rejects missing and malformed pagination metadata", async () => {
    installFetchMock([
      { match: "/missing-meta", response: apiResponse([], 200, null) },
      {
        match: "/bad-meta",
        response: apiResponse([], 200, { limit: "50", offset: 0, total: 0 }),
      },
    ]);

    await expect(apiListRequest("/missing-meta")).rejects.toBeInstanceOf(ContractError);
    await expect(apiListRequest("/bad-meta")).rejects.toBeInstanceOf(ContractError);
  });

  it("fetchAllPages rejects a page whose reported offset is not the requested offset", async () => {
    installFetchMock([
      {
        match: /\/items\?limit=2&offset=0/,
        response: apiResponse([{ id: "x" }], 200, {
          limit: 2,
          offset: 1,
          total: 2,
        }),
      },
    ]);

    await expect(fetchAllPages("/items", { limit: 2 })).rejects.toBeInstanceOf(
      ContractError
    );
  });

  it("fetches an authenticated binary resource without forcing a download", async () => {
    const requestSpy = vi.fn();
    installFetchMock([
      {
        match: "/notes/figure-1/raw",
        response: (request) => {
          requestSpy(request);
          return binaryResponse({
            body: "figure-bytes",
            contentType: "image/png",
            disposition: "attachment; filename*=UTF-8''panel%20A.png",
          });
        },
      },
    ]);

    const resource = await fetchProtectedBlobResource({
      path: "/notes/figure-1/raw",
      token: "secret-token",
    });

    expect(requestSpy).toHaveBeenCalledTimes(1);
    expect(requestSpy.mock.calls[0][0].init.headers).toEqual({
      Accept: "*/*",
      Authorization: "Bearer secret-token",
    });
    expect(resource.contentType).toBe("image/png");
    expect(resource.filename).toBe("panel A.png");
    expect(resource.blob).toBeInstanceOf(Blob);
    expect(resource.blob.size).toBe("figure-bytes".length);
    expect(resource.blob.type).toBe("image/png");
  });

  it("does not reject authentication when a protected blob read is opaquely absent", async () => {
    const authRejected = vi.fn();
    window.addEventListener(AUTH_REJECTED_EVENT, authRejected);
    installFetchMock([
      {
        match: "/visualizations/hidden/file/download",
        response: errorResponse("Visualization does not exist.", 404),
      },
    ]);

    try {
      await expect(
        fetchProtectedBlobResource({
          path: "/visualizations/hidden/file/download",
          token: "still-valid-token",
        })
      ).rejects.toMatchObject({
        message: "Visualization does not exist.",
        status: 404,
      });
      expect(authRejected).not.toHaveBeenCalled();
    } finally {
      window.removeEventListener(AUTH_REJECTED_EVENT, authRejected);
    }
  });

  it("rejects authentication when a protected blob read reports an expired token", async () => {
    const authRejected = vi.fn();
    const token = "expired-token";
    window.addEventListener(AUTH_REJECTED_EVENT, authRejected);
    installFetchMock([
      {
        match: "/visualizations/protected/file/download",
        response: errorResponse("Token expired.", 401),
      },
    ]);

    try {
      await expect(
        fetchProtectedBlobResource({
          path: "/visualizations/protected/file/download",
          token,
        })
      ).rejects.toMatchObject({
        message: "Token expired.",
        status: 401,
      });
      expect(authRejected).toHaveBeenCalledTimes(1);
      expect(authRejected.mock.calls[0][0].detail).toEqual({
        message: "Token expired.",
        status: 401,
        token,
      });
    } finally {
      window.removeEventListener(AUTH_REJECTED_EVENT, authRejected);
    }
  });

  it.each([
    "Invalid device token.",
    "Invalid personal access token.",
    "Session has been revoked.",
  ])("rejects authentication for any credential 401 (%s)", async (message) => {
    const authRejected = vi.fn();
    const token = "revoked-credential";
    window.addEventListener(AUTH_REJECTED_EVENT, authRejected);
    installFetchMock([{ match: "/notes", response: errorResponse(message, 401) }]);

    try {
      await expect(apiRequest("/notes", { token })).rejects.toMatchObject({
        message,
        status: 401,
      });
      expect(authRejected).toHaveBeenCalledTimes(1);
      expect(authRejected.mock.calls[0][0].detail).toEqual({ message, status: 401, token });
    } finally {
      window.removeEventListener(AUTH_REJECTED_EVENT, authRejected);
    }
  });

  it("does not reject the session for a credential-free 401", async () => {
    // Unauthenticated flows (device pairing, invitation redemption, login) can
    // return 401 while a user is signed in; they carry no session credential.
    const authRejected = vi.fn();
    window.addEventListener(AUTH_REJECTED_EVENT, authRejected);
    installFetchMock([
      {
        match: "/auth/devices/consume",
        method: "POST",
        response: errorResponse("Enrollment offer has expired.", 401),
      },
    ]);

    try {
      await expect(
        apiRequest("/auth/devices/consume", { method: "POST", body: { offer_token: "x" } })
      ).rejects.toMatchObject({ message: "Enrollment offer has expired.", status: 401 });
      expect(authRejected).not.toHaveBeenCalled();
    } finally {
      window.removeEventListener(AUTH_REJECTED_EVENT, authRejected);
    }
  });

  it.each([
    "Project contributor access required.",
    "Insufficient role.",
    "This action is not permitted for paired devices.",
  ])("keeps the session on a permission 403 (%s)", async (message) => {
    const authRejected = vi.fn();
    window.addEventListener(AUTH_REJECTED_EVENT, authRejected);
    installFetchMock([
      { match: "/notes", method: "POST", response: errorResponse(message, 403) },
    ]);

    try {
      await expect(
        apiRequest("/notes", { method: "POST", body: {}, token: "still-valid-token" })
      ).rejects.toMatchObject({ message, status: 403 });
      expect(authRejected).not.toHaveBeenCalled();
    } finally {
      window.removeEventListener(AUTH_REJECTED_EVENT, authRejected);
    }
  });
});
