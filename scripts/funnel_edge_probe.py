#!/usr/bin/env python3
"""Record the public Tailscale Funnel path to a Lab Tracker host over time.

A Funnel stall like issue #211 (TCP accepted, TLS ServerHello never sent)
happens before a request reaches Lab Tracker, so the app's logs never see it,
and by the time someone reports it the host's Tailscale logs may be gone. This
probe runs on the Funnel host and appends one JSON line per run recording:

* each public URL, resolved through a public DNS resolver (never MagicDNS, which
  on the host itself answers with a tailnet address) and checked address by
  address, stage by stage: TCP connect, TLS handshake, HTTP response;
* the node's Tailscale state (``tailscale status --json``) and its Serve/Funnel
  mappings (``tailscale serve status --json``);
* each loopback backend a mapping proxies to, checked directly; and
* the host's boot time, so reboots show up in the history.

Connections to the relay addresses come back to this node over Tailscale the
same way an outside client's do, so a check run here exercises the relay-to-node
leg. It cannot see problems on a client's own network.

``report`` summarizes failures, gaps between runs (host asleep or off, or the
agent not running), reboots, and Tailscale state changes. ``install-launchd``
runs the probe every few minutes as a macOS LaunchAgent.

Pure stdlib and Python 3.9 compatible, so launchd can run it with the system
``/usr/bin/python3``.
"""

from __future__ import annotations

import argparse
import gzip
import http.client
import ipaddress
import json
import os
import plistlib
import re
import shutil
import socket
import ssl
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.request
from collections import Counter
from collections.abc import Iterable, Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, NamedTuple
from urllib.parse import urlsplit

SCHEMA_VERSION = 1
LAUNCHD_LABEL = "com.lab-tracker.funnel-edge-probe"
DEFAULT_RESOLVER = "1.1.1.1"
DEFAULT_INTERVAL_MINUTES = 5
DEFAULT_GAP_MINUTES = 15
# About a week of five-minute runs per file; 26 gzipped files keep about six
# months of history in roughly 15 MB.
DEFAULT_MAX_BYTES = 5 * 1024 * 1024
DEFAULT_KEEP = 26
COMMAND_TIMEOUT = 10.0
BACKEND_TIMEOUT = 3.0
MAX_BODY_BYTES = 64 * 1024
USER_AGENT = "lab-tracker-funnel-edge-probe"
# launchd starts agents with PATH=/usr/bin:/bin:/usr/sbin:/sbin, which misses
# the Tailscale CLI on macOS.
_TAILSCALE_CANDIDATES = (
    "/usr/local/bin/tailscale",
    "/opt/homebrew/bin/tailscale",
    "/Applications/Tailscale.app/Contents/MacOS/Tailscale",
)
_LAUNCHD_PATH = "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
# Python 3.9 (the macOS system python3) does not yet alias socket.timeout to
# TimeoutError, so both are named.
_TIMEOUT_ERRORS = (socket.timeout, TimeoutError)  # noqa: UP041
_PEER_CLOSED_ERRORS = (
    ssl.SSLEOFError,
    ssl.SSLZeroReturnError,
    ConnectionResetError,
    BrokenPipeError,
    http.client.RemoteDisconnected,
)


class Timeouts(NamedTuple):
    connect: float = 5.0
    tls: float = 10.0
    http: float = 10.0


DEFAULT_TIMEOUTS = Timeouts()


def default_log_path() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Logs" / "lab-tracker" / "funnel-edge-probe.jsonl"
    state_home = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(state_home) / "lab-tracker" / "funnel-edge-probe.jsonl"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _elapsed_ms(started: float) -> int:
    return int(round((time.monotonic() - started) * 1000))


def parse_target(url: str) -> tuple[str, int, str]:
    """Return ``(host, port, health path)`` for a public https:// base URL."""
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        raise ValueError(f"expected an https:// URL, got {url!r}")
    path = parts.path.rstrip("/")
    if not path.endswith("/health"):
        path = f"{path}/health"
    return parts.hostname, parts.port or 443, path


def describe_error(exc: BaseException) -> str:
    """A short, stable description of why a stage failed."""
    if isinstance(exc, _TIMEOUT_ERRORS):
        return "timeout"
    if isinstance(exc, ssl.SSLCertVerificationError):
        return f"certificate: {exc.verify_message}"
    if isinstance(exc, _PEER_CLOSED_ERRORS):
        return "closed by peer"
    if isinstance(exc, ConnectionRefusedError):
        return "connection refused"
    if isinstance(exc, ssl.SSLError):
        return f"tls: {exc.reason or exc}"
    return (str(exc) or type(exc).__name__)[:200]


def _run(argv: list[str], timeout: float = COMMAND_TIMEOUT) -> tuple[str, str | None]:
    """Run a command; return ``(stdout, error)`` where error is None on success."""
    try:
        completed = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired:
        return "", f"timed out after {timeout:g}s"
    except OSError as exc:
        return "", describe_error(exc)
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip().splitlines()
        return completed.stdout, f"exit {completed.returncode}: {detail[-1] if detail else ''}"
    return completed.stdout, None


def parse_dig_output(text: str) -> list[str]:
    """Keep the address lines of ``dig +short`` output, which may list CNAMEs first."""
    addresses = []
    for line in text.splitlines():
        try:
            addresses.append(str(ipaddress.ip_address(line.strip())))
        except ValueError:
            continue
    return addresses


def resolve_public(host: str, resolver: str, *, ipv6: bool) -> dict[str, Any]:
    """Resolve through a public resolver, bypassing the host's MagicDNS view."""
    dig = shutil.which("dig")
    if dig is None:
        return {"resolver": resolver, "addresses": [], "error": "dig not found"}
    addresses: list[str] = []
    errors = []
    for record_type in ("A", "AAAA") if ipv6 else ("A",):
        stdout, error = _run(
            [dig, "+short", "+time=3", "+tries=2", record_type, host, f"@{resolver}"]
        )
        if error:
            errors.append(f"{record_type} {error}")
        else:
            addresses.extend(parse_dig_output(stdout))
    return {"resolver": resolver, "addresses": addresses, "error": "; ".join(errors) or None}


def resolve_system(host: str, port: int) -> list[str]:
    """What the host's own resolver says, for contrast (MagicDNS on a tailnet node)."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        return []
    return sorted({str(info[4][0]) for info in infos})


def read_http_response(sock: socket.socket, host: str, port: int, path: str) -> tuple[int, bytes]:
    authority = host if port == 443 else f"{host}:{port}"
    request = (
        f"GET {path} HTTP/1.1\r\nHost: {authority}\r\nUser-Agent: {USER_AGENT}\r\n"
        "Accept: application/json\r\nConnection: close\r\n\r\n"
    )
    sock.sendall(request.encode("ascii"))
    response = http.client.HTTPResponse(sock)
    try:
        response.begin()
        return response.status, response.read(MAX_BODY_BYTES)
    finally:
        response.close()


def health_identity(body: bytes) -> dict[str, str]:
    """Name the instance that answered, so a swapped port mapping is visible."""
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return {}
    app = payload.get("app") if isinstance(payload, dict) else None
    if not isinstance(app, dict):
        return {}
    identity = {}
    if isinstance(app.get("name"), str):
        identity["app"] = app["name"]
    if isinstance(app.get("source_revision"), str):
        identity["revision"] = app["source_revision"][:12]
    return identity


def _failed(result: dict[str, Any], stage: str, exc: BaseException, started: float) -> dict:
    result["outcome"] = stage
    result["error"] = describe_error(exc)
    result["failed_after_ms"] = _elapsed_ms(started)
    return result


def check_address(
    host: str,
    address: str,
    port: int,
    path: str,
    *,
    timeouts: Timeouts = DEFAULT_TIMEOUTS,
    context: ssl.SSLContext | None = None,
) -> dict[str, Any]:
    """Check one public address; ``outcome`` is "ok" or the stage that failed.

    A TCP connect that succeeds followed by a ``tls_handshake`` timeout is the
    signature from issue #211: the relay accepted the connection but nothing
    answered the ClientHello.
    """
    result: dict[str, Any] = {"address": address, "outcome": "ok", "error": None}
    started = time.monotonic()
    try:
        raw = socket.create_connection((address, port), timeout=timeouts.connect)
    except OSError as exc:
        return _failed(result, "tcp_connect", exc, started)
    result["connect_ms"] = _elapsed_ms(started)
    raw.settimeout(timeouts.tls)
    tls = (context or ssl.create_default_context()).wrap_socket(
        raw, server_hostname=host, do_handshake_on_connect=False
    )
    with tls:
        started = time.monotonic()
        try:
            tls.do_handshake()
        except OSError as exc:
            return _failed(result, "tls_handshake", exc, started)
        result["tls_ms"] = _elapsed_ms(started)
        result["tls_version"] = tls.version()
        tls.settimeout(timeouts.http)
        started = time.monotonic()
        try:
            status, body = read_http_response(tls, host, port, path)
        except (OSError, http.client.HTTPException) as exc:
            return _failed(result, "http", exc, started)
        result["response_ms"] = _elapsed_ms(started)
    result["status"] = status
    result.update(health_identity(body))
    if status != 200:
        result["outcome"] = "http"
        result["error"] = f"HTTP {status}"
    return result


def find_tailscale() -> str | None:
    found = shutil.which("tailscale")
    if found:
        return found
    for candidate in _TAILSCALE_CANDIDATES:
        if os.access(candidate, os.X_OK):
            return candidate
    return None


def _run_json(argv: list[str]) -> tuple[dict[str, Any], str | None]:
    stdout, error = _run(argv)
    if error:
        return {}, error
    try:
        payload = json.loads(stdout or "{}")
    except ValueError:
        return {}, "unparseable JSON"
    return (payload, None) if isinstance(payload, dict) else ({}, "unexpected JSON")


def summarize_tailscale_status(status: dict[str, Any]) -> dict[str, Any]:
    me = status.get("Self") or {}
    return {
        "backend_state": status.get("BackendState"),
        "online": me.get("Online"),
        "relay": me.get("Relay") or None,
        "dns_name": (me.get("DNSName") or "").rstrip(".") or None,
        "version": status.get("Version"),
        "health": list(status.get("Health") or []),
    }


def summarize_serve_config(config: dict[str, Any]) -> dict[str, Any]:
    """Map each served host:port to its handlers and whether Funnel exposes it.

    Mappings started without ``--bg`` live under ``Foreground`` and vanish when
    the command that started them exits, so they are marked.
    """
    sources = [(False, config)]
    sources.extend((True, entry) for entry in (config.get("Foreground") or {}).values())
    mappings: dict[str, Any] = {}
    for foreground, source in sources:
        allow = source.get("AllowFunnel") or {}
        for hostport, web in sorted((source.get("Web") or {}).items()):
            handlers = {
                path: handler.get("Proxy") or ",".join(sorted(handler))
                for path, handler in sorted((web.get("Handlers") or {}).items())
            }
            mapping = {"funnel": bool(allow.get(hostport)), "handlers": handlers}
            if foreground:
                mapping["foreground"] = True
            mappings[hostport] = mapping
    return mappings


def tailscale_snapshot() -> dict[str, Any]:
    cli = find_tailscale()
    if cli is None:
        return {"error": "tailscale CLI not found"}
    snapshot: dict[str, Any] = {}
    status, error = _run_json([cli, "status", "--json"])
    if error:
        snapshot["error"] = error
    else:
        snapshot.update(summarize_tailscale_status(status))
    serve, error = _run_json([cli, "serve", "status", "--json"])
    if error:
        snapshot["serve_error"] = error
    else:
        snapshot["serve"] = summarize_serve_config(serve)
    return snapshot


def loopback_backends(serve: dict[str, Any]) -> list[str]:
    targets = set()
    for mapping in serve.values():
        for target in mapping.get("handlers", {}).values():
            parts = urlsplit(target)
            if parts.scheme in ("http", "https") and parts.hostname in (
                "127.0.0.1",
                "localhost",
                "::1",
            ):
                targets.add(target.rstrip("/"))
    return sorted(targets)


def check_backend(target: str, timeout: float = BACKEND_TIMEOUT) -> dict[str, Any]:
    result: dict[str, Any] = {"target": target}
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request(f"{target}/health", headers={"User-Agent": USER_AGENT})
    started = time.monotonic()
    try:
        with opener.open(request, timeout=timeout) as response:
            result["status"] = response.status
            body = response.read(MAX_BODY_BYTES)
    except urllib.error.HTTPError as exc:
        result["status"] = exc.code
        body = b""
    except (OSError, http.client.HTTPException) as exc:
        reason = getattr(exc, "reason", None)
        result["error"] = describe_error(reason if isinstance(reason, BaseException) else exc)
        return result
    result["ms"] = _elapsed_ms(started)
    result.update(health_identity(body))
    return result


def host_boot_time() -> str | None:
    seconds = None
    if sys.platform == "darwin":
        stdout, _error = _run(["/usr/sbin/sysctl", "-n", "kern.boottime"])
        match = re.search(r"sec = (\d+)", stdout)
        if match:
            seconds = int(match.group(1))
    else:
        try:
            for line in Path("/proc/stat").read_text().splitlines():
                if line.startswith("btime "):
                    seconds = int(line.split()[1])
        except (OSError, ValueError):
            pass
    if seconds is None:
        return None
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat(timespec="seconds")


def run_probe(
    urls: list[str],
    *,
    resolver: str = DEFAULT_RESOLVER,
    ipv6: bool = True,
    timeouts: Timeouts = DEFAULT_TIMEOUTS,
) -> dict[str, Any]:
    started = time.monotonic()
    record: dict[str, Any] = {
        "schema": SCHEMA_VERSION,
        "at": _now(),
        "host": {"boot_time": host_boot_time()},
    }
    targets = [(url, *parse_target(url)) for url in urls]
    with ThreadPoolExecutor(max_workers=16) as pool:
        tailscale_future = pool.submit(tailscale_snapshot)
        lookups = [
            (
                url,
                host,
                port,
                path,
                pool.submit(resolve_public, host, resolver, ipv6=ipv6),
                pool.submit(resolve_system, host, port),
            )
            for url, host, port, path in targets
        ]
        pending = []
        for url, host, port, path, dns_future, view_future in lookups:
            dns = dns_future.result()
            checks = [
                pool.submit(check_address, host, address, port, path, timeouts=timeouts)
                for address in dns["addresses"]
            ]
            entry = {"url": url, "dns": dns, "host_view": view_future.result()}
            pending.append((entry, checks))
        tailscale = tailscale_future.result()
        backends = [
            pool.submit(check_backend, target)
            for target in loopback_backends(tailscale.get("serve") or {})
        ]
        public = []
        for entry, checks in pending:
            entry["checks"] = [future.result() for future in checks]
            entry["ok"] = bool(entry["checks"]) and all(
                check["outcome"] == "ok" for check in entry["checks"]
            )
            public.append(entry)
        record["tailscale"] = tailscale
        record["backends"] = [future.result() for future in backends]
    record["public"] = public
    record["ok"] = all(entry["ok"] for entry in public)
    record["probe_ms"] = _elapsed_ms(started)
    return record


def rotated_path(log: Path, index: int) -> Path:
    return log.with_name(f"{log.name}.{index}.gz")


def rotate_if_needed(log: Path, *, max_bytes: int, keep: int) -> bool:
    try:
        if log.stat().st_size < max_bytes:
            return False
    except FileNotFoundError:
        return False
    for index in range(keep - 1, 0, -1):
        source = rotated_path(log, index)
        if source.exists():
            source.replace(rotated_path(log, index + 1))
    with log.open("rb") as source, gzip.open(rotated_path(log, 1), "wb") as target:
        shutil.copyfileobj(source, target)
    log.unlink()
    return True


def append_record(log: Path, record: dict[str, Any], *, max_bytes: int, keep: int) -> None:
    log.parent.mkdir(parents=True, exist_ok=True)
    rotate_if_needed(log, max_bytes=max_bytes, keep=keep)
    with log.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, separators=(",", ":")) + "\n")


def describe_failure(entry: dict[str, Any]) -> str | None:
    """Summarize why one public URL failed in one run, or None when it passed."""
    checks = entry.get("checks") or []
    if not checks:
        return f"dns: {(entry.get('dns') or {}).get('error') or 'no public addresses'}"
    failed = Counter(
        f"{check.get('outcome')} {check.get('error') or ''}".strip()
        for check in checks
        if check.get("outcome") != "ok"
    )
    if not failed:
        return None
    return ", ".join(f"{kind} ({count}/{len(checks)} addresses)" for kind, count in failed.items())


def summarize_record(record: dict[str, Any]) -> str:
    if record.get("error"):
        return f"{record['at']} ERROR {record['error']}"
    failures = [
        f"{entry['url']} {describe_failure(entry)}"
        for entry in record.get("public", [])
        if not entry.get("ok")
    ]
    return f"{record['at']} " + ("ok" if not failures else "FAIL " + "; ".join(failures))


def _rotation_index(path: Path) -> int:
    try:
        return int(path.name.rsplit(".", 2)[-2])
    except (IndexError, ValueError):
        return 0


def iter_records(log: Path) -> Iterator[dict[str, Any]]:
    """Yield records oldest first, across the rotated files and the live log."""
    paths = sorted(log.parent.glob(f"{log.name}.*.gz"), key=_rotation_index, reverse=True)
    if log.exists():
        paths.append(log)
    for path in paths:
        opener = gzip.open if path.suffix == ".gz" else open
        with opener(path, "rt", encoding="utf-8") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if isinstance(record, dict) and isinstance(record.get("at"), str):
                    yield record


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.astimezone()


def _local(moment: datetime) -> str:
    return moment.astimezone().strftime("%Y-%m-%d %H:%M")


def _duration(delta: timedelta) -> str:
    minutes = int(delta.total_seconds() // 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    parts = [f"{days}d"] if days else []
    if hours:
        parts.append(f"{hours}h")
    if minutes or not parts:
        parts.append(f"{minutes}m")
    return "".join(parts)


def _tailscale_state(record: dict[str, Any]) -> str:
    tailscale = record.get("tailscale") or {}
    if tailscale.get("error"):
        return f"unavailable ({tailscale['error']})"
    state = str(tailscale.get("backend_state"))
    return state if tailscale.get("online") else f"{state}, offline"


def _funnel_ports(record: dict[str, Any]) -> list[str]:
    serve = (record.get("tailscale") or {}).get("serve") or {}
    return sorted(hostport for hostport, mapping in serve.items() if mapping.get("funnel"))


def _unhealthy_backends(record: dict[str, Any]) -> list[str]:
    return [
        f"{backend['target']} {backend.get('error') or 'HTTP ' + str(backend.get('status'))}"
        for backend in record.get("backends") or []
        if backend.get("error") or backend.get("status") != 200
    ]


def build_report(
    records: Iterable[dict[str, Any]],
    *,
    gap_minutes: float = DEFAULT_GAP_MINUTES,
    since: datetime | None = None,
) -> list[str]:
    """Return human-readable lines: failures, gaps, reboots, and state changes."""
    events: list[tuple[datetime, str]] = []
    open_incidents: dict[str, dict[str, Any]] = {}
    previous: dict[str, Any] | None = None
    previous_at: datetime | None = None
    first_at: datetime | None = None
    runs = 0

    def close(url: str) -> None:
        incident = open_incidents.pop(url)
        span = _local(incident["start"])
        if incident["end"] != incident["start"]:
            span += f" to {_local(incident['end'])}"
        kinds = "; ".join(kind for kind, _count in incident["kinds"].most_common())
        detail = f"tailscale {incident['tailscale']}"
        if incident["backends"]:
            detail += "; backend " + ", ".join(incident["backends"])
        runs_text = "1 run" if incident["runs"] == 1 else f"{incident['runs']} runs"
        events.append((incident["start"], f"{span} ({runs_text})  {url}  {kinds}  [{detail}]"))

    for record in records:
        at = _parse_time(record.get("at"))
        if at is None or (since is not None and at < since):
            continue
        runs += 1
        first_at = first_at or at
        if previous is not None and previous_at is not None:
            gap = at - previous_at
            if gap > timedelta(minutes=gap_minutes):
                events.append(
                    (
                        previous_at,
                        f"{_local(previous_at)} to {_local(at)}  no probe runs for "
                        f"{_duration(gap)}: host asleep or off, or the agent was not running",
                    )
                )
            boot = (record.get("host") or {}).get("boot_time")
            if boot and boot != (previous.get("host") or {}).get("boot_time"):
                booted = _parse_time(boot) or at
                events.append((booted, f"{_local(booted)}  host booted"))
            before, after = _tailscale_state(previous), _tailscale_state(record)
            if before != after:
                events.append((at, f"{_local(at)}  tailscale {before} -> {after}"))
            before_health = set((previous.get("tailscale") or {}).get("health") or [])
            for message in (record.get("tailscale") or {}).get("health") or []:
                if message not in before_health:
                    events.append((at, f"{_local(at)}  tailscale health: {message}"))
            if "serve" in (record.get("tailscale") or {}) and "serve" in (
                previous.get("tailscale") or {}
            ):
                before_ports, after_ports = _funnel_ports(previous), _funnel_ports(record)
                if before_ports != after_ports:
                    events.append(
                        (
                            at,
                            f"{_local(at)}  funnel ports {', '.join(before_ports) or 'none'}"
                            f" -> {', '.join(after_ports) or 'none'}",
                        )
                    )
        if record.get("error"):
            events.append((at, f"{_local(at)}  probe error: {record['error']}"))
        failing = {}
        for entry in record.get("public") or []:
            if not entry.get("ok"):
                failing[entry.get("url", "?")] = describe_failure(entry) or "failed"
        for url in [url for url in open_incidents if url not in failing]:
            close(url)
        for url, kind in failing.items():
            incident = open_incidents.setdefault(
                url,
                {
                    "start": at,
                    "runs": 0,
                    "kinds": Counter(),
                    "tailscale": _tailscale_state(record),
                    "backends": _unhealthy_backends(record),
                },
            )
            incident["end"] = at
            incident["runs"] += 1
            incident["kinds"][kind] += 1
        previous, previous_at = record, at
    for url in list(open_incidents):
        close(url)

    if runs == 0 or first_at is None or previous_at is None:
        return ["No probe records found."]
    lines = [f"{runs} probe runs from {_local(first_at)} to {_local(previous_at)} (local time)"]
    if not events:
        lines.append("No failures, gaps, reboots, or Tailscale state changes.")
    lines.extend(text for _moment, text in sorted(events, key=lambda event: event[0]))
    return lines


def render_launchd_plist(
    *,
    python: str,
    script: str,
    urls: list[str],
    log: Path,
    interval_seconds: int,
    resolver: str = DEFAULT_RESOLVER,
    ipv6: bool = True,
) -> bytes:
    arguments = [python, script, "probe", "--log", str(log), "--resolver", resolver]
    if not ipv6:
        arguments.append("--no-ipv6")
    for url in urls:
        arguments.extend(["--url", url])
    return plistlib.dumps(
        {
            "Label": LAUNCHD_LABEL,
            "ProgramArguments": arguments,
            "StartInterval": interval_seconds,
            "RunAtLoad": True,
            # The default launchd limits throttle the job enough to inflate
            # recorded connect and TLS times several-fold; a run is about a
            # second of work every few minutes.
            "ProcessType": "Interactive",
            "StandardOutPath": "/dev/null",
            "StandardErrorPath": str(log.with_name("funnel-edge-probe.stderr.log")),
            "EnvironmentVariables": {"PATH": _LAUNCHD_PATH},
        }
    )


def _validated_urls(urls: list[str]) -> list[str]:
    for url in urls:
        try:
            parse_target(url)
        except ValueError as exc:
            raise SystemExit(f"funnel_edge_probe: {exc}") from exc
    return urls


def _cmd_probe(args: argparse.Namespace) -> int:
    urls = _validated_urls(args.url)
    timeouts = Timeouts(args.connect_timeout, args.tls_timeout, args.http_timeout)
    try:
        record = run_probe(urls, resolver=args.resolver, ipv6=args.ipv6, timeouts=timeouts)
    except Exception as exc:  # record the failure rather than leave a silent gap
        traceback.print_exc()
        record = {"schema": SCHEMA_VERSION, "at": _now(), "error": repr(exc)[:500]}
    append_record(args.log, record, max_bytes=args.max_bytes, keep=args.keep)
    print(summarize_record(record))
    return 0


def _cmd_report(args: argparse.Namespace) -> int:
    since = _parse_time(args.since) if args.since else None
    if args.since and since is None:
        raise SystemExit(
            f"funnel_edge_probe: --since must be an ISO date or time, got {args.since!r}"
        )
    for line in build_report(iter_records(args.log), gap_minutes=args.gap_minutes, since=since):
        print(line)
    return 0


def _cmd_install_launchd(args: argparse.Namespace) -> int:
    urls = _validated_urls(args.url)
    if sys.platform != "darwin" and not args.dry_run:
        raise SystemExit("funnel_edge_probe: launchd is macOS-only; run `probe` from cron instead.")
    support = Path.home() / "Library" / "Application Support" / "lab-tracker"
    script = support / "funnel_edge_probe.py"
    plist = render_launchd_plist(
        python=args.python,
        script=str(script),
        urls=urls,
        log=args.log,
        interval_seconds=args.interval_minutes * 60,
        resolver=args.resolver,
        ipv6=args.ipv6,
    )
    if args.dry_run:
        sys.stdout.write(plist.decode("utf-8"))
        return 0
    # Run a copy, so switching branches in the checkout cannot break the agent.
    support.mkdir(parents=True, exist_ok=True)
    shutil.copy2(Path(__file__).resolve(), script)
    args.log.parent.mkdir(parents=True, exist_ok=True)
    plist_path = Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    plist_path.write_bytes(plist)
    subprocess.run(["plutil", "-lint", str(plist_path)], check=True, capture_output=True)
    domain = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", f"{domain}/{LAUNCHD_LABEL}"], capture_output=True)
    for attempt in range(3):
        loaded = subprocess.run(
            ["launchctl", "bootstrap", domain, str(plist_path)], capture_output=True, text=True
        )
        if loaded.returncode == 0:
            break
        time.sleep(1 + attempt)
    else:
        raise SystemExit(f"funnel_edge_probe: launchctl bootstrap failed: {loaded.stderr.strip()}")
    print(f"Installed LaunchAgent {LAUNCHD_LABEL} (every {args.interval_minutes} min).")
    print(f"  script: {script}")
    print(f"  plist:  {plist_path}")
    print(f"  log:    {args.log}")
    print(f"Report: {args.python} '{script}' report --log '{args.log}'")
    print(f"Remove: launchctl bootout {domain}/{LAUNCHD_LABEL}; rm '{plist_path}'")
    return 0


def _default_python() -> str:
    if sys.platform == "darwin" and os.access("/usr/bin/python3", os.X_OK):
        return "/usr/bin/python3"
    return sys.executable


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add_log(p: argparse.ArgumentParser) -> None:
        p.add_argument("--log", type=Path, default=default_log_path(), help="JSON-lines log")

    def add_targets(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--url",
            action="append",
            required=True,
            help="public https:// base URL; repeat once per Funnel port",
        )
        p.add_argument("--resolver", default=DEFAULT_RESOLVER, help="public DNS resolver")
        p.add_argument(
            "--no-ipv6",
            dest="ipv6",
            action="store_false",
            help="skip AAAA addresses (for hosts without IPv6)",
        )

    p = sub.add_parser("probe", help="check each URL once and append a record")
    add_targets(p)
    add_log(p)
    p.add_argument("--connect-timeout", type=float, default=DEFAULT_TIMEOUTS.connect)
    p.add_argument("--tls-timeout", type=float, default=DEFAULT_TIMEOUTS.tls)
    p.add_argument("--http-timeout", type=float, default=DEFAULT_TIMEOUTS.http)
    p.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES, help="rotate above this")
    p.add_argument("--keep", type=int, default=DEFAULT_KEEP, help="rotated files to keep")
    p.set_defaults(func=_cmd_probe)

    p = sub.add_parser("report", help="summarize failures, gaps, reboots, state changes")
    add_log(p)
    p.add_argument("--since", help="ISO date or time; local time when no offset is given")
    p.add_argument("--gap-minutes", type=float, default=DEFAULT_GAP_MINUTES)
    p.set_defaults(func=_cmd_report)

    p = sub.add_parser("install-launchd", help="run the probe as a macOS LaunchAgent")
    add_targets(p)
    add_log(p)
    p.add_argument("--interval-minutes", type=int, default=DEFAULT_INTERVAL_MINUTES)
    p.add_argument("--python", default=_default_python(), help="interpreter launchd runs")
    p.add_argument("--dry-run", action="store_true", help="print the plist and stop")
    p.set_defaults(func=_cmd_install_launchd)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if getattr(args, "keep", 1) < 1:
        raise SystemExit("funnel_edge_probe: --keep must be at least 1")
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
