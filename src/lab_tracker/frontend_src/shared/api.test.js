import { describe, expect, it } from "vitest";

import { apiListRequest, apiRequest, fetchAllPages } from "./api.js";
import { ContractError } from "./contract.js";
import { apiResponse, installFetchMock } from "../test/utils.js";

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

});
