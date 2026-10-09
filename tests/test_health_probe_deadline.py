"""The /health probes have a total deadline, not only per-phase timeouts (GH #239).

httpx timeouts bound each connect, write, and read separately, so a server that
sends its headers and then trickles the body a byte at a time never trips them.
The advisory probes behind ``lt setup status`` and the ``lt-mcp`` startup check
must not be held open that way.
"""

from __future__ import annotations

import gzip
import json
import socket
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager

import httpx
import pytest

from lab_tracker import mcp_server
from lab_tracker.client_release import ReleaseIdentity
from lab_tracker.mcp_api_client import (
    LabTrackerAPIClient,
    LabTrackerAPIUnavailableError,
    MCPSettings,
)
from lab_tracker_client import setup as setup_helpers
from lab_tracker_client import transport
from lab_tracker_client.transport import (
    HEALTH_PROBE_DEADLINE_SECONDS,
    HEALTH_PROBE_MAX_BODY_BYTES,
    request_within_deadline,
)

SERVER_REVISION = "b" * 40
HEALTH_BODY = json.dumps(
    {"status": "ok", "app": {"version": "0.9.0", "source_revision": SERVER_REVISION}}
).encode()
# Total drip time is far past every deadline used below, but each byte arrives
# well inside any per-phase timeout, which is exactly what the deadline is for.
DRIP_BYTES = 120
DRIP_INTERVAL_SECONDS = 0.05
TEST_DEADLINE_SECONDS = 0.3
# A deadline test that took this long did not enforce the deadline.
ELAPSED_LIMIT_SECONDS = 3.0

Handler = Callable[[socket.socket, threading.Event], None]


@contextmanager
def raw_server(handler: Handler) -> Iterator[str]:
    """Serve one connection with ``handler`` on loopback; yields the base URL."""

    stop = threading.Event()
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        listener.settimeout(10)

        def serve() -> None:
            try:
                connection, _ = listener.accept()
            except OSError:
                return
            with connection:
                connection.settimeout(10)
                try:
                    connection.recv(65536)
                    handler(connection, stop)
                except OSError:
                    pass

        worker = threading.Thread(target=serve, daemon=True)
        worker.start()
        try:
            yield f"http://127.0.0.1:{listener.getsockname()[1]}"
        finally:
            stop.set()
            worker.join(timeout=10)


def _response_head(*headers: str) -> bytes:
    lines = ["HTTP/1.1 200 OK", "Content-Type: application/json", "Connection: close", *headers]
    return ("\r\n".join(lines) + "\r\n\r\n").encode()


def answer(body: bytes, *headers: str) -> Handler:
    def handler(connection: socket.socket, _stop: threading.Event) -> None:
        connection.sendall(_response_head(f"Content-Length: {len(body)}", *headers) + body)

    return handler


def drip(connection: socket.socket, stop: threading.Event) -> None:
    """Send the headers at once, then the body one byte at a time."""

    connection.sendall(_response_head(f"Content-Length: {DRIP_BYTES}"))
    for _ in range(DRIP_BYTES):
        if stop.is_set():
            return
        connection.sendall(b" ")
        time.sleep(DRIP_INTERVAL_SECONDS)


def drip_headers(connection: socket.socket, stop: threading.Event) -> None:
    """Send the response head one byte at a time, so no header ever completes."""

    head = _response_head("Content-Length: 0", "X-Pad: " + "a" * DRIP_BYTES)
    for offset in range(len(head)):
        if stop.is_set():
            return
        connection.sendall(head[offset : offset + 1])
        time.sleep(DRIP_INTERVAL_SECONDS)


@pytest.fixture(autouse=True)
def loopback_only(monkeypatch: pytest.MonkeyPatch) -> None:
    # Keep the loopback server off any sandbox or CI proxy.
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")


def test_the_drip_outlasts_the_deadline_that_bounds_it() -> None:
    assert DRIP_BYTES * DRIP_INTERVAL_SECONDS > TEST_DEADLINE_SECONDS * 10


def test_the_default_deadline_is_a_few_seconds_over_a_small_body() -> None:
    assert 0 < HEALTH_PROBE_DEADLINE_SECONDS <= 10
    assert 0 < HEALTH_PROBE_MAX_BODY_BYTES <= 1024 * 1024


def test_a_prompt_response_comes_back_whole() -> None:
    with raw_server(answer(HEALTH_BODY)) as url, httpx.Client(timeout=2.0) as client:
        response = request_within_deadline(
            client, "GET", url + "/health", deadline_seconds=HEALTH_PROBE_DEADLINE_SECONDS
        )

    assert response.status_code == 200
    assert response.json()["app"]["version"] == "0.9.0"
    assert response.headers["content-type"] == "application/json"
    assert str(response.request.url) == url + "/health"


def test_an_encoded_response_is_returned_decoded_once() -> None:
    encoded = gzip.compress(HEALTH_BODY)
    with (
        raw_server(answer(encoded, "Content-Encoding: gzip")) as url,
        httpx.Client(timeout=2.0) as client,
    ):
        response = request_within_deadline(
            client, "GET", url + "/health", deadline_seconds=HEALTH_PROBE_DEADLINE_SECONDS
        )

    assert response.json()["status"] == "ok"
    assert response.content == HEALTH_BODY
    assert "content-encoding" not in response.headers


def test_a_body_past_the_cap_is_cut_at_the_cap() -> None:
    body = b"x" * 5000
    with raw_server(answer(body)) as url, httpx.Client(timeout=2.0) as client:
        response = request_within_deadline(
            client,
            "GET",
            url + "/health",
            deadline_seconds=HEALTH_PROBE_DEADLINE_SECONDS,
            max_body_bytes=1024,
        )

    assert response.status_code == 200
    assert response.content == body[:1024]


def test_a_trickled_body_hits_the_deadline_not_the_per_phase_timeout() -> None:
    with raw_server(drip) as url, httpx.Client(timeout=2.0) as client:
        started = time.monotonic()
        with pytest.raises(httpx.ReadTimeout) as caught:
            request_within_deadline(
                client, "GET", url + "/health", deadline_seconds=TEST_DEADLINE_SECONDS
            )
        elapsed = time.monotonic() - started

    assert elapsed < ELAPSED_LIMIT_SECONDS
    assert f"{TEST_DEADLINE_SECONDS:g} seconds" in str(caught.value)


def test_trickled_headers_hit_the_deadline_not_the_per_phase_timeout() -> None:
    with raw_server(drip_headers) as url, httpx.Client(timeout=2.0) as client:
        started = time.monotonic()
        with pytest.raises(httpx.ReadTimeout) as caught:
            request_within_deadline(
                client, "GET", url + "/health", deadline_seconds=TEST_DEADLINE_SECONDS
            )
        elapsed = time.monotonic() - started

    assert elapsed < ELAPSED_LIMIT_SECONDS
    assert f"{TEST_DEADLINE_SECONDS:g} seconds" in str(caught.value)


class StalledTransport(httpx.BaseTransport):
    """Blocks a request until its client is closed, then fails as ``failure`` builds it."""

    def __init__(self, failure: Callable[[], Exception]) -> None:
        self._failure = failure
        self._closed = threading.Event()

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        assert self._closed.wait(timeout=10), "the watchdog never closed the client"
        raise self._failure()

    def close(self) -> None:
        self._closed.set()


@pytest.mark.parametrize(
    "failure",
    [
        lambda: httpx.ReadTimeout("timed out"),
        # A plain socket closed under a read raises a read error; a TLS one reports
        # that the server disconnected.
        lambda: httpx.ReadError("Bad file descriptor"),
        lambda: httpx.RemoteProtocolError("Server disconnected without sending a response."),
    ],
    ids=["read-timeout", "read-error", "tls-disconnect"],
)
def test_a_read_the_watchdog_closed_reads_as_the_deadline(
    failure: Callable[[], Exception],
) -> None:
    with httpx.Client(transport=StalledTransport(failure)) as client:
        started = time.monotonic()
        with pytest.raises(httpx.ReadTimeout) as caught:
            request_within_deadline(
                client, "GET", "http://lab.invalid/health", deadline_seconds=TEST_DEADLINE_SECONDS
            )
        elapsed = time.monotonic() - started

    assert elapsed < ELAPSED_LIMIT_SECONDS
    assert f"{TEST_DEADLINE_SECONDS:g} seconds" in str(caught.value)


def test_a_connect_failure_after_the_deadline_keeps_its_own_error() -> None:
    transport = StalledTransport(lambda: httpx.ConnectTimeout("connect timed out"))
    with httpx.Client(transport=transport) as client, pytest.raises(httpx.ConnectTimeout):
        request_within_deadline(
            client, "GET", "http://lab.invalid/health", deadline_seconds=TEST_DEADLINE_SECONDS
        )


def test_a_read_timeout_before_the_deadline_keeps_its_own_error() -> None:
    failure = httpx.ReadTimeout("shorter read timeout")

    def fail(_request: httpx.Request) -> httpx.Response:
        raise failure

    with (
        httpx.Client(transport=httpx.MockTransport(fail)) as client,
        pytest.raises(httpx.ReadTimeout) as caught,
    ):
        request_within_deadline(client, "GET", "http://lab.invalid/health", deadline_seconds=2)
    assert caught.value is failure


def test_an_expired_read_timeout_does_not_depend_on_timer_scheduling(monkeypatch) -> None:
    now = [100.0]

    class DelayedTimer:
        def __init__(self, _interval, _callback):
            pass

        def start(self):
            pass

        def cancel(self):
            pass

    def fail(_request: httpx.Request) -> httpx.Response:
        now[0] += 1
        raise httpx.ReadTimeout("timed out")

    monkeypatch.setattr(transport.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(transport.threading, "Timer", DelayedTimer)
    with (
        httpx.Client(transport=httpx.MockTransport(fail)) as client,
        pytest.raises(httpx.ReadTimeout, match="did not finish within 0.3 seconds"),
    ):
        request_within_deadline(
            client, "GET", "http://lab.invalid/health", deadline_seconds=TEST_DEADLINE_SECONDS
        )


def test_a_response_that_finishes_inside_the_deadline_is_not_cut_by_the_watchdog() -> None:
    with raw_server(answer(HEALTH_BODY)) as url, httpx.Client(timeout=2.0) as client:
        response = request_within_deadline(
            client, "GET", url + "/health", deadline_seconds=TEST_DEADLINE_SECONDS
        )
        time.sleep(TEST_DEADLINE_SECONDS * 2)

        # The finished request must not leave a timer that closes the client later.
        assert not client.is_closed

    assert response.json()["status"] == "ok"


def test_setup_status_probe_reports_a_trickled_response_instead_of_waiting(
    monkeypatch,
) -> None:
    monkeypatch.setattr(setup_helpers, "HEALTH_PROBE_DEADLINE_SECONDS", TEST_DEADLINE_SECONDS)
    with raw_server(drip) as url:
        started = time.monotonic()
        result = setup_helpers.probe_health_diagnostics(url)
        elapsed = time.monotonic() - started

    assert elapsed < ELAPSED_LIMIT_SECONDS
    assert result["reachable"] is False
    assert result["diagnosis"] == "http_response_timeout"


def test_setup_status_probe_reports_trickled_headers_instead_of_waiting(monkeypatch) -> None:
    monkeypatch.setattr(setup_helpers, "HEALTH_PROBE_DEADLINE_SECONDS", TEST_DEADLINE_SECONDS)
    with raw_server(drip_headers) as url:
        started = time.monotonic()
        result = setup_helpers.probe_health_diagnostics(url)
        elapsed = time.monotonic() - started

    assert elapsed < ELAPSED_LIMIT_SECONDS
    assert result["reachable"] is False
    assert result["diagnosis"] == "http_response_timeout"


def test_setup_status_probe_still_reads_a_prompt_release() -> None:
    with raw_server(answer(HEALTH_BODY)) as url:
        result = setup_helpers.probe_health_diagnostics(url)

    assert result == {
        "reachable": True,
        "release": {"version": "0.9.0", "revision": SERVER_REVISION},
    }


def test_setup_status_probe_keeps_a_huge_reply_reachable_without_a_release() -> None:
    body = b"<html>" + b"x" * (HEALTH_PROBE_MAX_BODY_BYTES * 2)
    with raw_server(answer(body)) as url:
        result = setup_helpers.probe_health_diagnostics(url)

    assert result == {"reachable": True}


def test_mcp_client_health_deadline_raises_the_unavailable_error(monkeypatch) -> None:
    with raw_server(drip) as url:
        client = LabTrackerAPIClient(MCPSettings(base_url=url, timeout_seconds=2.0))
        try:
            started = time.monotonic()
            with pytest.raises(LabTrackerAPIUnavailableError) as caught:
                client.health(deadline_seconds=TEST_DEADLINE_SECONDS)
            elapsed = time.monotonic() - started
        finally:
            client.close()

    assert elapsed < ELAPSED_LIMIT_SECONDS
    assert caught.value.connection_diagnostic["diagnosis"] == "http_response_timeout"


def test_mcp_client_health_deadline_covers_trickled_headers() -> None:
    with raw_server(drip_headers) as url:
        client = LabTrackerAPIClient(MCPSettings(base_url=url, timeout_seconds=2.0))
        try:
            started = time.monotonic()
            with pytest.raises(LabTrackerAPIUnavailableError) as caught:
                client.health(deadline_seconds=TEST_DEADLINE_SECONDS)
            elapsed = time.monotonic() - started
        finally:
            client.close()

    assert elapsed < ELAPSED_LIMIT_SECONDS
    assert caught.value.connection_diagnostic["diagnosis"] == "http_response_timeout"


def test_mcp_client_health_without_a_deadline_is_unchanged() -> None:
    with raw_server(answer(HEALTH_BODY)) as url:
        client = LabTrackerAPIClient(MCPSettings(base_url=url))
        try:
            health = client.health()
        finally:
            client.close()

    assert health["app"]["version"] == "0.9.0"


def test_mcp_client_health_sends_the_surface_header_within_a_deadline() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("x-labtracker-surface", ""))
        return httpx.Response(200, json={"status": "ok"})

    client = LabTrackerAPIClient(
        MCPSettings(base_url="http://127.0.0.1:9"), transport=httpx.MockTransport(handler)
    )
    try:
        assert client.health(deadline_seconds=HEALTH_PROBE_DEADLINE_SECONDS) == {"status": "ok"}
    finally:
        client.close()

    assert seen == ["mcp"]


def test_lt_mcp_startup_check_gives_up_on_a_trickled_response(monkeypatch, capsys) -> None:
    monkeypatch.setattr(mcp_server, "HEALTH_PROBE_DEADLINE_SECONDS", TEST_DEADLINE_SECONDS)
    monkeypatch.setattr(
        mcp_server,
        "installed_release",
        lambda: ReleaseIdentity(version="0.1.0", revision="a" * 40),
    )
    with raw_server(drip) as url:
        started = time.monotonic()
        notice = mcp_server.probe_client_update_notice(MCPSettings(base_url=url))
        elapsed = time.monotonic() - started

    assert notice is None
    assert elapsed < ELAPSED_LIMIT_SECONDS
    err = capsys.readouterr().err
    assert "starting without the update check" in err
    assert f"did not finish within {TEST_DEADLINE_SECONDS:g} seconds" in err


def test_lt_mcp_startup_check_gives_up_on_trickled_headers(monkeypatch, capsys) -> None:
    monkeypatch.setattr(mcp_server, "HEALTH_PROBE_DEADLINE_SECONDS", TEST_DEADLINE_SECONDS)
    monkeypatch.setattr(
        mcp_server,
        "installed_release",
        lambda: ReleaseIdentity(version="0.1.0", revision="a" * 40),
    )
    with raw_server(drip_headers) as url:
        started = time.monotonic()
        notice = mcp_server.probe_client_update_notice(MCPSettings(base_url=url))
        elapsed = time.monotonic() - started

    assert notice is None
    assert elapsed < ELAPSED_LIMIT_SECONDS
    err = capsys.readouterr().err
    assert "starting without the update check" in err
    assert f"did not finish within {TEST_DEADLINE_SECONDS:g} seconds" in err


def test_lt_mcp_startup_check_still_names_an_update_from_a_prompt_response(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        mcp_server,
        "installed_release",
        lambda: ReleaseIdentity(version="0.1.0", revision="a" * 40),
    )
    with raw_server(answer(HEALTH_BODY)) as url:
        notice = mcp_server.probe_client_update_notice(MCPSettings(base_url=url))

    assert notice is not None
    assert "server runs release 0.9.0" in notice
