"""Credential-free diagnostics from the request's own HTTPX trace events.

No extra connections, retries, DNS lookups, or Tailscale commands are made.
Unknown/custom transports degrade to an explicitly unclassified failure.
"""

from __future__ import annotations

import socket
import ssl
from typing import Any
from urllib.parse import urlsplit

import httpx


def _caused_by(exc: BaseException, kind: type[BaseException]) -> bool:
    seen: set[int] = set()
    while id(exc) not in seen:
        seen.add(id(exc))
        if isinstance(exc, kind):
            return True
        cause = exc.__cause__ or exc.__context__
        if cause is None:
            break
        exc = cause
    return False


class ConnectionTrace:
    """Observe stages, never retaining headers, URLs, credentials, or bodies."""

    def __init__(self, base_url: str) -> None:
        try:
            self.hostname = urlsplit(base_url).hostname or ""
        except ValueError:
            self.hostname = ""
        self.failed_stage = ""
        self.target_tls = False

    def __call__(self, event: str, info: dict[str, Any]) -> None:
        if event.endswith(".start_tls.started"):
            hostname = info.get("server_hostname", "")
            if isinstance(hostname, bytes):
                hostname = hostname.decode("ascii", errors="replace")
            self.target_tls = str(hostname).lower() == self.hostname.lower()
        elif event.endswith(".start_tls.failed"):
            self.failed_stage = "tls" if self.target_tls else "proxy_tls"
        elif event.endswith(".connect_tcp.failed"):
            self.failed_stage = "tcp"

    def diagnose(self, exc: Exception) -> dict[str, str]:
        diagnosis = "transport_error"
        detail = "The request failed; its connection stage could not be determined."
        next_step = "Check the configured server URL, network path, and Lab Tracker host."
        if _caused_by(exc, ssl.SSLCertVerificationError) or "CERTIFICATE_VERIFY_FAILED" in str(exc):
            diagnosis = "tls_certificate_error"
            detail = "TLS certificate verification failed."
            next_step = (
                "Check the server certificate, hostname, clock, and trusted CA "
                "configuration. Do not disable certificate verification."
            )
        elif self.failed_stage == "tls":
            if isinstance(exc, (httpx.TimeoutException, TimeoutError)):
                diagnosis = "tls_handshake_stalled"
                detail = (
                    "The TCP connection succeeded, but the TLS handshake did not complete "
                    "before the timeout."
                )
                next_step = (
                    "Ask the host operator to check the HTTPS listener and any reverse proxy."
                )
                if self.hostname.lower().rstrip(".").endswith(".ts.net"):
                    detail += (
                        " If this host uses Tailscale Funnel, its origin may be offline or not "
                        "serving; the client cannot confirm that cause."
                    )
                    next_step = (
                        "On the Lab Tracker host, check `tailscale funnel status` and confirm the "
                        "service is listening on the proxied port. Funnel clients do not need to "
                        "join the tailnet."
                    )
            else:
                diagnosis = "tls_handshake_failed"
                detail = "The TCP connection succeeded, but the TLS handshake failed."
                next_step = "Check the host's HTTPS listener and proxy TLS configuration."
        elif self.failed_stage == "proxy_tls" or isinstance(exc, httpx.ProxyError):
            diagnosis = "proxy_connection_failed"
            detail = "The configured HTTP proxy connection failed."
            next_step = "Check the client's proxy settings and the proxy service."
        elif _caused_by(exc, socket.gaierror):
            diagnosis = "dns_resolution_failed"
            detail = "The connection hostname could not be resolved."
            next_step = "Check the configured hostname and DNS resolver."
        elif self.failed_stage == "tcp":
            diagnosis = "tcp_connection_failed"
            detail = "A TCP connection could not be established."
            next_step = (
                "Check the server address and port, host availability, routing, and firewall."
            )
        elif isinstance(exc, httpx.ReadTimeout):
            diagnosis = "http_response_timeout"
            detail = "The connection was established, but the HTTP response timed out."
            next_step = "Ask the host operator to check the application and reverse-proxy logs."
        return {"diagnosis": diagnosis, "detail": detail, "next_step": next_step}


def connection_error_metadata(exc: Exception) -> dict[str, str]:
    """Additional fail-soft MCP fields, without replacing its existing detail."""
    diagnostic = getattr(exc, "connection_diagnostic", {})
    return {key: diagnostic[key] for key in ("diagnosis", "next_step") if key in diagnostic}
