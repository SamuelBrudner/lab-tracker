// Zero-dependency runtime validation for API response contracts.
//
// The transport layer (shared/api.js) is deliberately lenient: it converts a
// malformed successful envelope to null or an empty list. That is convenient
// for optional data but dangerous for the shapes the UI actually depends on —
// backend contract drift then surfaces as an empty result in one path and a
// render crash in another. These combinators let a domain gateway describe the
// representative shape it consumes and FAIL LOUDLY, with a single typed
// ContractError, when a 2xx payload does not match.
//
// A validator is `(value, path) => value`: it returns the (optionally copied)
// value on success and throws ContractError on mismatch. Objects validate the
// declared keys and pass unknown keys through unchanged, so additive backend
// changes never break a client that does not read the new field.

class ContractError extends Error {
  constructor(message, { path = "", expected = "", received } = {}) {
    super(message);
    this.name = "ContractError";
    this.path = path;
    this.expected = expected;
    this.received = received;
  }
}

function typeName(value) {
  if (value === null) {
    return "null";
  }
  if (Array.isArray(value)) {
    return "array";
  }
  return typeof value;
}

function violation(path, expected, received) {
  const where = path || "<root>";
  throw new ContractError(
    `Contract violation at ${where}: expected ${expected}, received ${typeName(received)}`,
    { path, expected, received }
  );
}

// Primitive validators.
function string(value, path = "") {
  return typeof value === "string" ? value : violation(path, "string", value);
}

function number(value, path = "") {
  return typeof value === "number" && !Number.isNaN(value)
    ? value
    : violation(path, "number", value);
}

function boolean(value, path = "") {
  return typeof value === "boolean" ? value : violation(path, "boolean", value);
}

// Accept any shape (including null/undefined) without inspection.
function unknown(value) {
  return value;
}

// Combinators.
function nullable(inner) {
  return (value, path = "") => (value === null ? null : inner(value, path));
}

function optional(inner) {
  return (value, path = "") => (value === undefined ? undefined : inner(value, path));
}

// Absent, null, or a valid inner value are all accepted.
function nullish(inner) {
  return optional(nullable(inner));
}

function arrayOf(inner) {
  return (value, path = "") => {
    if (!Array.isArray(value)) {
      violation(path, "array", value);
    }
    return value.map((item, index) => inner(item, `${path}[${index}]`));
  };
}

function oneOf(...allowed) {
  const label = `one of ${allowed.map((entry) => JSON.stringify(entry)).join(", ")}`;
  return (value, path = "") => (allowed.includes(value) ? value : violation(path, label, value));
}

// Validate the declared keys; carry unknown keys through unchanged.
function object(shape) {
  const keys = Object.keys(shape);
  return (value, path = "") => {
    if (value === null || typeof value !== "object" || Array.isArray(value)) {
      violation(path || "<root>", "object", value);
    }
    const result = { ...value };
    for (const key of keys) {
      result[key] = shape[key](value[key], path ? `${path}.${key}` : key);
    }
    return result;
  };
}

// Unwrap and validate a single-resource envelope `{ data: T }`.
function parseResource(envelope, itemValidator) {
  if (
    envelope === null ||
    typeof envelope !== "object" ||
    Array.isArray(envelope) ||
    !Object.prototype.hasOwnProperty.call(envelope, "data")
  ) {
    violation("", "an object envelope with a data property", envelope);
  }
  return itemValidator(envelope.data, "data");
}

// Unwrap and validate a collection envelope `{ data: T[], meta? }`.
function parseCollection(envelope, itemValidator) {
  if (envelope === null || typeof envelope !== "object" || Array.isArray(envelope)) {
    violation("", "an object envelope with a data array", envelope);
  }
  const data = envelope.data;
  if (!Array.isArray(data)) {
    violation("data", "array", data);
  }
  const items = data.map((item, index) => itemValidator(item, `data[${index}]`));
  const meta =
    envelope.meta && typeof envelope.meta === "object" && !Array.isArray(envelope.meta)
      ? envelope.meta
      : null;
  return { data: items, meta };
}

export {
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
};
