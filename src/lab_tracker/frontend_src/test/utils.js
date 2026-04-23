import { vi } from "vitest";

function createResponse(status, payload, extras = {}) {
  const { blob = null, contentType = "application/json", headers = {}, text = "" } = extras;
  return {
    headers: {
      get(name) {
        const normalized = String(name).toLowerCase();
        if (normalized in headers) {
          return headers[normalized];
        }
        if (normalized === "content-type") {
          return contentType;
        }
        return null;
      },
    },
    blob: async () => blob,
    json: async () => payload,
    ok: status >= 200 && status < 300,
    status,
    text: async () => text,
  };
}

function apiResponse(data, status = 200, meta = null) {
  return createResponse(status, meta ? { data, meta } : { data });
}

function errorResponse(message, status = 400) {
  return createResponse(status, {
    error: {
      message,
    },
  });
}

function binaryResponse({
  body,
  contentType = "application/octet-stream",
  disposition = "",
  status = 200,
}) {
  const blob = body instanceof Blob ? body : new Blob([body], { type: contentType });
  const headers = {};
  if (disposition) {
    headers["content-disposition"] = disposition;
  }
  return createResponse(status, null, {
    blob,
    contentType,
    headers,
  });
}

function matchesRoute(route, method, url) {
  if ((route.method || "GET").toUpperCase() !== method) {
    return false;
  }
  if (typeof route.match === "string") {
    return route.match === url;
  }
  return route.match.test(url);
}

function nextResponse(route, request) {
  if (Array.isArray(route.response)) {
    if (route.response.length === 0) {
      throw new Error(`No mocked responses left for ${request.method} ${request.url}`);
    }
    return route.response.shift();
  }
  if (typeof route.response === "function") {
    return route.response(request);
  }
  return route.response;
}

function installFetchMock(routes) {
  const fetchMock = vi.fn(async (input, init = {}) => {
    const url = typeof input === "string" ? input : input.url;
    const method = String(init.method || "GET").toUpperCase();
    const request = { init, method, url };

    const route = routes.find((candidate) => matchesRoute(candidate, method, url));
    if (!route) {
      throw new Error(`Unexpected fetch: ${method} ${url}`);
    }

    return nextResponse(route, request);
  });

  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

export { apiResponse, binaryResponse, errorResponse, installFetchMock };
