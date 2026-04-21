function parseApiError(payload, fallbackMessage) {
  if (!payload || typeof payload !== "object") {
    return fallbackMessage;
  }
  if (payload.error && payload.error.message) {
    return payload.error.message;
  }
  return fallbackMessage;
}

function isJsonResponse(response) {
  return (response.headers.get("content-type") || "").includes("application/json");
}

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

async function throwApiError(response) {
  const payload = await parseErrorPayload(response);
  const error = new Error(parseApiError(payload, `Request failed with ${response.status}`));
  error.status = response.status;
  error.payload = payload;
  throw error;
}

function buildRequestHeaders({ token = "", body = null, accept = "application/json" } = {}) {
  const isFormData = typeof FormData !== "undefined" && body instanceof FormData;
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

async function apiFetch(path, options = {}) {
  const { method = "GET", token = "", body = null } = options;
  const { headers, isFormData } = buildRequestHeaders({ body, token });

  const response = await fetch(path, {
    method,
    headers,
    body: body === null ? undefined : isFormData ? body : JSON.stringify(body),
  });

  if (!response.ok) {
    await throwApiError(response);
  }

  if (!isJsonResponse(response)) {
    return null;
  }
  return response.json();
}

async function apiRequest(path, options = {}) {
  const payload = await apiFetch(path, options);
  if (!payload || !Object.prototype.hasOwnProperty.call(payload, "data")) {
    return null;
  }
  return payload.data;
}

async function apiListRequest(path, options = {}) {
  const payload = await apiFetch(path, options);
  const data = Array.isArray(payload?.data) ? payload.data : [];
  const meta =
    payload && typeof payload.meta === "object" && payload.meta !== null
      ? payload.meta
      : {
          limit: data.length,
          offset: 0,
          total: data.length,
        };
  return { data, meta };
}

async function fetchAllPages(path, options = {}) {
  const { limit = 200, ...requestOptions } = options;
  const items = [];
  let offset = 0;
  let total = null;

  while (true) {
    const { data, meta } = await apiListRequest(
      buildApiPath(path, { limit, offset }),
      requestOptions
    );
    items.push(...data);

    const resolvedLimit =
      typeof meta?.limit === "number" && meta.limit > 0 ? meta.limit : limit;
    const resolvedOffset = typeof meta?.offset === "number" ? meta.offset : offset;
    if (typeof meta?.total === "number") {
      total = meta.total;
    }

    if (data.length === 0) {
      break;
    }
    if (total !== null && items.length >= total) {
      break;
    }
    if (data.length < resolvedLimit) {
      break;
    }

    offset = resolvedOffset + data.length;
  }

  return items;
}

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

async function downloadProtectedResource({ path, token = "", filename = "" }) {
  const { headers } = buildRequestHeaders({ token, accept: "*/*" });
  const response = await fetch(path, {
    method: "GET",
    headers,
  });

  if (!response.ok) {
    await throwApiError(response);
  }

  const blob = await response.blob();
  const resolvedFilename =
    filename ||
    parseContentDispositionFilename(response.headers.get("content-disposition")) ||
    "download";

  const objectUrl = URL.createObjectURL(blob);
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
  apiRequest,
  apiListRequest,
  buildApiPath,
  downloadProtectedResource,
  fetchAllPages,
  parseApiError,
};
