import { demoFetch, isStaticDemoEnabled } from "./static-demo-api.js";
import { ContractError, parseCollection, parseResource, unknown } from "./contract.js";

/** @typedef {Error & {payload: unknown, status: number}} ApiError */
/**
 * @typedef {{
 *   accept?: string,
 *   body?: unknown,
 *   method?: string,
 *   notifyAuthRejected?: boolean,
 *   token?: string,
 * }} ApiOptions
 */

const AUTH_REJECTED_EVENT = "lab-tracker:auth-rejected";
const AUTH_REJECTION_MESSAGE_PATTERN =
  /auth(entication|orization)? required|authorization header|credential|invalid token|missing authorization|session|token (has )?expired|unrecognized token/i;

/** @param {unknown} payload @param {string} fallbackMessage */
function parseApiError(payload, fallbackMessage) {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    return fallbackMessage;
  }
  const error = /** @type {Record<string, unknown>} */ (payload).error;
  if (error && typeof error === "object" && !Array.isArray(error)) {
    const message = /** @type {Record<string, unknown>} */ (error).message;
    if (typeof message === "string" && message) {
      return message;
    }
  }
  return fallbackMessage;
}

/** @param {Response} response */
function isJsonResponse(response) {
  return (response.headers.get("content-type") || "").includes("application/json");
}

/** @param {Response} response */
async function parseErrorPayload(response) {
  if (!isJsonResponse(response)) {
    return null;
  }
  try {
    return await response.json();
  } catch {
    return null;
  }
}

/** @param {ApiError} error @param {string} token */
function notifyAuthRejected(error, token) {
  if (typeof window === "undefined" || typeof window.dispatchEvent !== "function") {
    return;
  }
  window.dispatchEvent(
    new CustomEvent(AUTH_REJECTED_EVENT, {
      detail: {
        message: error.message,
        status: error.status,
        token,
      },
    })
  );
}

/** @param {unknown} message */
function isAuthRejectedMessage(message) {
  return AUTH_REJECTION_MESSAGE_PATTERN.test(String(message || ""));
}

/**
 * @param {Response} response
 * @param {{notifyAuthRejected?: boolean, token?: string}} [options]
 * @returns {Promise<never>}
 */
async function throwApiError(
  response,
  { notifyAuthRejected: shouldNotify = false, token = "" } = {}
) {
  const payload = await parseErrorPayload(response);
  const error = /** @type {ApiError} */ (
    new Error(parseApiError(payload, `Request failed with ${response.status}`))
  );
  error.status = response.status;
  error.payload = payload;
  if (shouldNotify && response.status === 401 && isAuthRejectedMessage(error.message)) {
    notifyAuthRejected(error, token);
  }
  throw error;
}

/** @param {{token?: string, body?: unknown, accept?: string}} [options] */
function buildRequestHeaders({ token = "", body = null, accept = "application/json" } = {}) {
  const isFormData = typeof FormData !== "undefined" && body instanceof FormData;
  /** @type {Record<string, string>} */
  const headers = {
    Accept: accept,
  };

  if (token) {
    headers.Authorization = `Bearer ${token}`;
  }
  if (body !== null && !isFormData) {
    headers["Content-Type"] = "application/json";
  }

  return { headers, isFormData };
}

/** @param {string} path @param {Record<string, unknown>} [params] */
function buildApiPath(path, params = {}) {
  const url = new URL(path, "http://lab-tracker.local");
  Object.entries(params).forEach(([key, value]) => {
    if (value === null || value === undefined || value === "") {
      url.searchParams.delete(key);
      return;
    }
    url.searchParams.set(key, String(value));
  });
  return `${url.pathname}${url.search}${url.hash}`;
}

/** @param {string} path @param {RequestInit} [init] @returns {Promise<Response>} */
function appFetch(path, init = {}) {
  if (isStaticDemoEnabled()) {
    return demoFetch(path, init);
  }
  return fetch(path, init);
}

/** @param {string} path @param {ApiOptions} [options] @returns {Promise<unknown>} */
async function apiFetch(path, options = {}) {
  const {
    method = "GET",
    token = "",
    body = null,
    accept = "application/json",
    notifyAuthRejected = true,
  } = options;
  const { headers, isFormData } = buildRequestHeaders({ accept, body, token });

  const response = await appFetch(path, {
    method,
    headers,
    body:
      body === null
        ? undefined
        : isFormData
          ? /** @type {FormData} */ (body)
          : JSON.stringify(body),
  });

  if (!response.ok) {
    await throwApiError(response, {
      notifyAuthRejected: notifyAuthRejected && Boolean(token) && !isStaticDemoEnabled(),
      token,
    });
  }

  if (!isJsonResponse(response)) {
    return null;
  }
  return response.json();
}

/** @param {string} path @param {ApiOptions} [options] */
async function apiRequest(path, options = {}) {
  const payload = await apiFetch(path, options);
  return parseResource(payload, unknown);
}

/** @param {string} path @param {ApiOptions} [options] */
async function apiTextRequest(path, options = {}) {
  const {
    method = "GET",
    token = "",
    body = null,
    accept = "text/plain",
    notifyAuthRejected = true,
  } = options;
  const { headers, isFormData } = buildRequestHeaders({ accept, body, token });
  const response = await appFetch(path, {
    method,
    headers,
    body:
      body === null
        ? undefined
        : isFormData
          ? /** @type {FormData} */ (body)
          : JSON.stringify(body),
  });

  if (!response.ok) {
    await throwApiError(response, {
      notifyAuthRejected: notifyAuthRejected && Boolean(token) && !isStaticDemoEnabled(),
      token,
    });
  }

  return response.text();
}

/** @param {string} path @param {ApiOptions} [options] */
async function apiListRequest(path, options = {}) {
  const payload = await apiFetch(path, options);
  return parseCollection(payload, unknown);
}

/** @param {string} path @param {ApiOptions & {limit?: number}} [options] */
async function fetchAllPages(path, options = {}) {
  const { limit = 200, ...requestOptions } = options;
  const items = [];
  let offset = 0;

  while (true) {
    const { data, meta } = await apiListRequest(
      buildApiPath(path, { limit, offset }),
      requestOptions
    );
    items.push(...data);

    if (meta.offset !== offset) {
      throw new ContractError(
        `Contract violation at meta.offset: expected requested offset ${offset}, received ${meta.offset}`,
        {
          expected: `requested offset ${offset}`,
          path: "meta.offset",
          received: meta.offset,
        }
      );
    }
    const resolvedLimit = meta.limit;
    const resolvedOffset = meta.offset;
    if (data.length === 0) {
      break;
    }
    if (items.length >= meta.total) {
      break;
    }
    if (data.length < resolvedLimit) {
      break;
    }

    offset = resolvedOffset + data.length;
  }

  return items;
}

/** @param {unknown} headerValue */
function parseContentDispositionFilename(headerValue) {
  const value = String(headerValue || "");
  if (!value) {
    return "";
  }

  const encodedMatch = value.match(/filename\*\s*=\s*([^;]+)/i);
  if (encodedMatch) {
    const encodedValue = encodedMatch[1].trim();
    const parts = encodedValue.split("''");
    const candidate = parts.length === 2 ? parts[1] : encodedValue;
    try {
      return decodeURIComponent(candidate.replace(/^"|"$/g, ""));
    } catch {
      return candidate.replace(/^"|"$/g, "");
    }
  }

  const filenameMatch = value.match(/filename\s*=\s*"([^"]+)"/i);
  if (filenameMatch) {
    return filenameMatch[1];
  }

  const bareMatch = value.match(/filename\s*=\s*([^;]+)/i);
  return bareMatch ? bareMatch[1].trim().replace(/^"|"$/g, "") : "";
}

/** @param {{path: string, token?: string}} options */
async function fetchProtectedBlobResource({ path, token = "" }) {
  const { headers } = buildRequestHeaders({ token, accept: "*/*" });
  const response = await appFetch(path, {
    method: "GET",
    headers,
  });

  if (!response.ok) {
    await throwApiError(response, {
      notifyAuthRejected: Boolean(token) && !isStaticDemoEnabled(),
      token,
    });
  }

  const blob = await response.blob();
  return {
    blob,
    contentType:
      response.headers.get("content-type") || blob.type || "application/octet-stream",
    filename: parseContentDispositionFilename(response.headers.get("content-disposition")),
  };
}

/** @param {{path: string, token?: string, filename?: string}} options */
async function downloadProtectedResource({ path, token = "", filename = "" }) {
  const resource = await fetchProtectedBlobResource({ path, token });
  const resolvedFilename = filename || resource.filename || "download";

  const objectUrl = URL.createObjectURL(resource.blob);
  const anchor = document.createElement("a");
  anchor.href = objectUrl;
  anchor.download = resolvedFilename;
  anchor.style.display = "none";
  document.body.appendChild(anchor);
  try {
    anchor.click();
  } finally {
    anchor.remove();
    window.setTimeout(() => URL.revokeObjectURL(objectUrl), 0);
  }

  return { filename: resolvedFilename };
}

export {
  AUTH_REJECTED_EVENT,
  apiFetch,
  apiRequest,
  apiTextRequest,
  apiListRequest,
  buildApiPath,
  downloadProtectedResource,
  fetchProtectedBlobResource,
  fetchAllPages,
  isStaticDemoEnabled,
  parseApiError,
};
