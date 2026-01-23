"""Minimal FastAPI shim for local development without external dependencies."""

from __future__ import annotations

from dataclasses import dataclass
import importlib.machinery
import importlib.util
from pathlib import Path
import sys
from typing import Any, Callable


def _load_real_fastapi():
    package_root = Path(__file__).resolve().parent.parent
    search_paths = [path for path in sys.path if Path(path).resolve() != package_root]
    spec = importlib.machinery.PathFinder.find_spec("fastapi", search_paths)
    if spec and spec.loader:
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    return None


_real_fastapi = _load_real_fastapi()
if _real_fastapi is not None and hasattr(_real_fastapi, "FastAPI"):
    FastAPI = _real_fastapi.FastAPI
    __all__ = ["FastAPI"]
else:
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

    __all__ = ["FastAPI"]
