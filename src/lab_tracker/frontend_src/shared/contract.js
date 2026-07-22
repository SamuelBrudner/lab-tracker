// @ts-check

// Zero-dependency runtime validation for API response contracts.
//
// Every JSON resource/list helper fails loudly on malformed successful
// envelopes. These combinators additionally let a domain gateway describe the
// representative shape it consumes, so backend contract drift produces one
// typed ContractError at the network boundary rather than null/empty data or a
// later render crash.
//
// A validator is `(value, path) => value`: it returns the (optionally copied)
// value on success and throws ContractError on mismatch. Objects validate the
// declared keys and pass unknown keys through unchanged, so additive backend
// changes never break a client that does not read the new field.

/** @template T @typedef {(value: unknown, path?: string) => T} Validator */

class ContractError extends Error {
  /**
   * @param {string} message
   * @param {{path?: string, expected?: string, received?: unknown}} [details]
   */
  constructor(message, { path = "", expected = "", received } = {}) {
    super(message);
    this.name = "ContractError";
    this.path = path;
    this.expected = expected;
    this.received = received;
  }
}

/** @param {unknown} value */
function typeName(value) {
  if (value === null) {
    return "null";
  }
  if (Array.isArray(value)) {
    return "array";
  }
  return typeof value;
}

/**
 * @param {string} path
 * @param {string} expected
 * @param {unknown} received
 * @returns {never}
 */
function violation(path, expected, received) {
  const where = path || "<root>";
  throw new ContractError(
    `Contract violation at ${where}: expected ${expected}, received ${typeName(received)}`,
    { path, expected, received }
  );
}

// Primitive validators.
/** @type {Validator<string>} */
function string(value, path = "") {
  return typeof value === "string" ? value : violation(path, "string", value);
}

/** @type {Validator<number>} */
function number(value, path = "") {
  return typeof value === "number" && !Number.isNaN(value)
    ? value
    : violation(path, "number", value);
}

/** @type {Validator<boolean>} */
function boolean(value, path = "") {
  return typeof value === "boolean" ? value : violation(path, "boolean", value);
}

// Accept any shape (including null/undefined) without inspection.
/** @type {Validator<unknown>} */
function unknown(value) {
  return value;
}

/** @type {Validator<number>} */
function integer(value, path = "") {
  return typeof value === "number" && Number.isInteger(value)
    ? value
    : violation(path, "integer", value);
}

/** @type {Validator<number>} */
function nonNegativeInteger(value, path = "") {
  const result = integer(value, path);
  return result >= 0 ? result : violation(path, "non-negative integer", value);
}

/** @type {Validator<number>} */
function positiveInteger(value, path = "") {
  const result = integer(value, path);
  return result >= 1 ? result : violation(path, "positive integer", value);
}

// Combinators.
/**
 * @template T
 * @param {Validator<T>} inner
 * @returns {Validator<T | null>}
 */
function nullable(inner) {
  return (value, path = "") => (value === null ? null : inner(value, path));
}

/**
 * @template T
 * @param {Validator<T>} inner
 * @returns {Validator<T | undefined>}
 */
function optional(inner) {
  return (value, path = "") => (value === undefined ? undefined : inner(value, path));
}

// Absent, null, or a valid inner value are all accepted.
/**
 * @template T
 * @param {Validator<T>} inner
 * @returns {Validator<T | null | undefined>}
 */
function nullish(inner) {
  return optional(nullable(inner));
}

/**
 * @template T
 * @param {Validator<T>} inner
 * @returns {Validator<T[]>}
 */
function arrayOf(inner) {
  return (value, path = "") => {
    if (!Array.isArray(value)) {
      violation(path, "array", value);
    }
    return value.map((item, index) => inner(item, `${path}[${index}]`));
  };
}

/**
 * @template T
 * @param {...T} allowed
 * @returns {Validator<T>}
 */
function oneOf(...allowed) {
  const label = `one of ${allowed.map((entry) => JSON.stringify(entry)).join(", ")}`;
  return (value, path = "") =>
    allowed.some((entry) => Object.is(entry, value))
      ? /** @type {T} */ (value)
      : violation(path, label, value);
}

// Validate the declared keys; carry unknown keys through unchanged.
/**
 * @template {Record<string, Validator<unknown>>} TShape
 * @param {TShape} shape
 * @returns {Validator<Record<string, unknown>>}
 */
function object(shape) {
  const keys = Object.keys(shape);
  return (value, path = "") => {
    if (value === null || typeof value !== "object" || Array.isArray(value)) {
      violation(path || "<root>", "object", value);
    }
    const record = /** @type {Record<string, unknown>} */ (value);
    const validators = /** @type {Record<string, Validator<unknown>>} */ (shape);
    const result = { ...record };
    for (const key of keys) {
      result[key] = validators[key](record[key], path ? `${path}.${key}` : key);
    }
    return result;
  };
}

// Unwrap and validate a single-resource envelope `{ data: T }`.
/**
 * @template T
 * @param {unknown} envelope
 * @param {Validator<T>} itemValidator
 * @returns {T}
 */
function parseResource(envelope, itemValidator) {
  if (
    envelope === null ||
    typeof envelope !== "object" ||
    Array.isArray(envelope) ||
    !Object.prototype.hasOwnProperty.call(envelope, "data")
  ) {
    violation("", "an object envelope with a data property", envelope);
  }
  const record = /** @type {Record<string, unknown>} */ (envelope);
  return itemValidator(record.data, "data");
}

/**
 * @typedef {{limit: number, offset: number, total: number}} PaginationMeta
 */

/** @type {Validator<PaginationMeta>} */
const paginationMetaShape = /** @type {Validator<PaginationMeta>} */ (
  object({
    limit: positiveInteger,
    offset: nonNegativeInteger,
    total: nonNegativeInteger,
  })
);

/**
 * Validate a resource envelope and a required metadata contract together.
 *
 * @template TData
 * @template TMeta
 * @param {unknown} envelope
 * @param {Validator<TData>} itemValidator
 * @param {Validator<TMeta>} metaValidator
 * @returns {{data: TData, meta: TMeta}}
 */
function parseResourceWithMeta(envelope, itemValidator, metaValidator) {
  if (envelope === null || typeof envelope !== "object" || Array.isArray(envelope)) {
    violation("", "an object envelope with data and meta properties", envelope);
  }
  if (!Object.prototype.hasOwnProperty.call(envelope, "data")) {
    violation("data", "present property", undefined);
  }
  if (!Object.prototype.hasOwnProperty.call(envelope, "meta")) {
    violation("meta", "present property", undefined);
  }
  const record = /** @type {Record<string, unknown>} */ (envelope);
  return {
    data: itemValidator(record.data, "data"),
    meta: metaValidator(record.meta, "meta"),
  };
}

/**
 * Unwrap and validate a paginated collection envelope. Pagination metadata is
 * mandatory for every list response; accepting a missing/partial meta object
 * makes fetchAllPages silently truncate or loop over a drifted API.
 *
 * @template T
 * @param {unknown} envelope
 * @param {Validator<T>} itemValidator
 * @returns {{data: T[], meta: PaginationMeta}}
 */
function parseCollection(envelope, itemValidator) {
  if (envelope === null || typeof envelope !== "object" || Array.isArray(envelope)) {
    violation("", "an object envelope with a data array", envelope);
  }
  const record = /** @type {Record<string, unknown>} */ (envelope);
  const data = record.data;
  if (!Array.isArray(data)) {
    violation("data", "array", data);
  }
  const items = data.map((item, index) => itemValidator(item, `data[${index}]`));
  const meta = paginationMetaShape(record.meta, "meta");
  if (data.length > meta.limit) {
    violation("data", `at most meta.limit (${meta.limit}) items`, data);
  }
  if (data.length > meta.total) {
    violation("meta.total", `at least the returned item count (${data.length})`, meta.total);
  }
  return { data: items, meta };
}

export {
  ContractError,
  arrayOf,
  boolean,
  integer,
  nullable,
  nonNegativeInteger,
  nullish,
  number,
  object,
  oneOf,
  optional,
  paginationMetaShape,
  parseCollection,
  parseResource,
  parseResourceWithMeta,
  positiveInteger,
  string,
  unknown,
};
