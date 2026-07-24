import { describe, expect, it, vi } from "vitest";

import {
  AUTH_REJECTED_EVENT,
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

});
