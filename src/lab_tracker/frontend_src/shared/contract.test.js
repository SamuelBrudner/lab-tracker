import { describe, expect, it } from "vitest";

import {
  ContractError,
  arrayOf,
  boolean,
  nullable,
  nullish,
  number,
  object,
  oneOf,
  optional,
  parseCollection,
  parseResource,
  string,
  unknown,
} from "./contract.js";

describe("contract primitives", () => {
  it("accepts matching primitives and returns them", () => {
    expect(string("hi")).toBe("hi");
    expect(number(3)).toBe(3);
    expect(boolean(true)).toBe(true);
    expect(unknown({ any: "thing" })).toEqual({ any: "thing" });
  });

  it("throws a ContractError with path/expected/received on mismatch", () => {
    let caught;
    try {
      string(42, "field");
    } catch (err) {
      caught = err;
    }
    expect(caught).toBeInstanceOf(ContractError);
    expect(caught.path).toBe("field");
    expect(caught.expected).toBe("string");
    expect(caught.received).toBe(42);
    expect(caught.message).toContain("field");
  });

  it("rejects NaN as a number", () => {
    expect(() => number(Number.NaN)).toThrow(ContractError);
  });
});

describe("contract combinators", () => {
  it("nullable admits null but still checks non-null", () => {
    expect(nullable(string)(null)).toBeNull();
    expect(nullable(string)("x")).toBe("x");
    expect(() => nullable(string)(3)).toThrow(ContractError);
  });

  it("optional admits undefined only", () => {
    expect(optional(string)(undefined)).toBeUndefined();
    expect(() => optional(string)(null)).toThrow(ContractError);
  });

  it("nullish admits undefined and null", () => {
    expect(nullish(string)(undefined)).toBeUndefined();
    expect(nullish(string)(null)).toBeNull();
    expect(nullish(string)("x")).toBe("x");
  });

  it("arrayOf validates each element with an indexed path", () => {
    expect(arrayOf(number)([1, 2])).toEqual([1, 2]);
    let caught;
    try {
      arrayOf(number)([1, "bad"], "nums");
    } catch (err) {
      caught = err;
    }
    expect(caught).toBeInstanceOf(ContractError);
    expect(caught.path).toBe("nums[1]");
  });

  it("oneOf restricts to the allowed set", () => {
    expect(oneOf("a", "b")("b")).toBe("b");
    expect(() => oneOf("a", "b")("c")).toThrow(ContractError);
  });

  it("object validates declared keys and passes unknown keys through", () => {
    const shape = object({ id: string, size: optional(number) });
    const result = shape({ id: "x", size: 2, extra: "kept" });
    expect(result).toEqual({ id: "x", size: 2, extra: "kept" });
  });

  it("object rejects a missing required key and a non-object", () => {
    const shape = object({ id: string });
    expect(() => shape({})).toThrow(ContractError);
    expect(() => shape(null)).toThrow(ContractError);
    expect(() => shape([1])).toThrow(ContractError);
  });
});

describe("envelope parsers", () => {
  const itemShape = object({ id: string });

  it("parseResource unwraps and validates data", () => {
    expect(parseResource({ data: { id: "x", n: 1 } }, itemShape)).toEqual({ id: "x", n: 1 });
  });

  it("parseResource throws when the envelope has no data property", () => {
    expect(() => parseResource(null, itemShape)).toThrow(ContractError);
    expect(() => parseResource({}, itemShape)).toThrow(ContractError);
    expect(() => parseResource({ data: { wrong: true } }, itemShape)).toThrow(ContractError);
  });

  it("parseCollection validates items and carries meta", () => {
    const result = parseCollection(
      { data: [{ id: "a" }, { id: "b" }], meta: { total: 2 } },
      itemShape
    );
    expect(result.data).toEqual([{ id: "a" }, { id: "b" }]);
    expect(result.meta).toEqual({ total: 2 });
  });

  it("parseCollection throws when data is not an array (contract drift)", () => {
    let caught;
    try {
      parseCollection({ data: { id: "a" } }, itemShape);
    } catch (err) {
      caught = err;
    }
    expect(caught).toBeInstanceOf(ContractError);
    expect(caught.path).toBe("data");
  });

  it("parseCollection defaults meta to null when absent", () => {
    expect(parseCollection({ data: [] }, itemShape).meta).toBeNull();
  });
});
