"""FastAPI compatibility layer with a local fallback."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

try:  # pragma: no cover - exercised when FastAPI is installed.
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
except ModuleNotFoundError:  # pragma: no cover - fallback for offline environments.

    @dataclass
    class _Response:
        status_code: int
        payload: Any

        def json(self) -> Any:
            return self.payload

    class FastAPI:
        def __init__(self, title: str | None = None) -> None:
            self.title = title or ""
            self._routes: dict[tuple[str, str], Callable[[], Any]] = {}

        def get(self, path: str) -> Callable[[Callable[[], Any]], Callable[[], Any]]:
            def decorator(func: Callable[[], Any]) -> Callable[[], Any]:
                self._routes[("GET", path)] = func
                return func

            return decorator

        def _handle(self, method: str, path: str) -> _Response:
            handler = self._routes.get((method, path))
            if handler is None:
                return _Response(status_code=404, payload={"detail": "Not Found"})
            return _Response(status_code=200, payload=handler())

    class TestClient:
        __test__ = False

        def __init__(self, app: FastAPI) -> None:
            self._app = app

        def get(self, path: str):
            return self._app._handle("GET", path)
