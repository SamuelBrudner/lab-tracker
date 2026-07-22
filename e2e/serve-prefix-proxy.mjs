// Test-only reverse proxy for exercising the app beneath a real URL prefix.
//
// Requests below /lab-tracker are forwarded to the ordinary auth-disabled E2E
// server with that prefix removed. The production shell and API gateways still
// initiate some root-absolute requests, so root requests are redirected into
// the prefix before being forwarded. A 307 is intentional: it preserves method
// and body for API writes instead of silently turning them into GET requests.
import { createServer, request as requestUpstream } from "node:http";

const PREFIX = "/lab-tracker";
const HOST = "127.0.0.1";
const proxyPort = Number(process.env.E2E_PREFIX_PORT || "8179");
const upstreamPort = Number(process.env.E2E_UPSTREAM_PORT || "8177");

function isPrefixed(pathname) {
  return pathname === PREFIX || pathname.startsWith(`${PREFIX}/`);
}

function prefixedLocation(rawUrl) {
  const normalized = rawUrl.startsWith("/") ? rawUrl : `/${rawUrl}`;
  return `${PREFIX}${normalized}`;
}

const server = createServer((incoming, outgoing) => {
  const rawUrl = incoming.url || "/";
  const url = new URL(rawUrl, `http://${HOST}:${proxyPort}`);

  if (!isPrefixed(url.pathname)) {
    outgoing.writeHead(307, { Location: prefixedLocation(rawUrl) });
    outgoing.end();
    return;
  }

  const strippedPath = url.pathname.slice(PREFIX.length) || "/";
  const headers = {
    ...incoming.headers,
    host: `${HOST}:${upstreamPort}`,
    "x-forwarded-prefix": PREFIX,
  };
  delete headers["proxy-connection"];

  const upstream = requestUpstream(
    {
      hostname: HOST,
      port: upstreamPort,
      method: incoming.method,
      path: `${strippedPath}${url.search}`,
      headers,
    },
    (response) => {
      outgoing.writeHead(response.statusCode || 502, response.headers);
      response.pipe(outgoing);
    }
  );

  upstream.on("error", (error) => {
    if (outgoing.headersSent) {
      outgoing.destroy(error);
      return;
    }
    outgoing.writeHead(502, { "Content-Type": "text/plain; charset=utf-8" });
    outgoing.end(`Prefix proxy could not reach the E2E server: ${error.message}\n`);
  });
  incoming.on("aborted", () => upstream.destroy());
  incoming.pipe(upstream);
});

function shutdown() {
  server.close(() => process.exit(0));
}

process.on("SIGTERM", shutdown);
process.on("SIGINT", shutdown);

server.listen(proxyPort, HOST);
