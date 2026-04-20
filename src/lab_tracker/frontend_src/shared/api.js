function parseApiError(payload, fallbackMessage) {
  if (!payload || typeof payload !== "object") {
    return fallbackMessage;
  }
  if (payload.error && payload.error.message) {
    return payload.error.message;
  }
  return fallbackMessage;
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
  const isFormData = typeof FormData !== "undefined" && body instanceof FormData;
  const headers = {
    Accept: "application/json",
  };

  if (token) {
    headers.Authorization = `Bearer ${token}`;
  }
  if (body !== null && !isFormData) {
    headers["Content-Type"] = "application/json";
  }

  const response = await fetch(path, {
    method,
    headers,
    body: body === null ? undefined : isFormData ? body : JSON.stringify(body),
  });

  const isJson = (response.headers.get("content-type") || "").includes("application/json");
  const payload = isJson ? await response.json() : null;

  if (!response.ok) {
    const error = new Error(parseApiError(payload, `Request failed with ${response.status}`));
    error.status = response.status;
    error.payload = payload;
    throw error;
  }

  return payload;
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

export { apiRequest, apiListRequest, buildApiPath, fetchAllPages, parseApiError };
