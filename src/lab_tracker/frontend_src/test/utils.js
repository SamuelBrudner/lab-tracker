import { vi } from "vitest";

function createResponse(status, payload) {
  return {
    headers: {
      get(name) {
        if (String(name).toLowerCase() === "content-type") {
          return "application/json";
        }
        return null;
      },
    },
    json: async () => payload,
    ok: status >= 200 && status < 300,
    status,
  };
}

function apiResponse(data, status = 200) {
  return createResponse(status, { data });
}

function errorResponse(message, status = 400) {
  return createResponse(status, {
    error: {
      message,
    },
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

export { apiResponse, errorResponse, installFetchMock };
