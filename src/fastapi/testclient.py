"""Minimal TestClient shim for FastAPI."""

from __future__ import annotations

import importlib.machinery
import importlib.util
from pathlib import Path
import sys

from fastapi import FastAPI


def _load_real_testclient():
    package_root = Path(__file__).resolve().parent.parent
    search_paths = [path for path in sys.path if Path(path).resolve() != package_root]
    spec = importlib.machinery.PathFinder.find_spec("fastapi.testclient", search_paths)
    if spec and spec.loader:
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    return None


_real_testclient = _load_real_testclient()


if _real_testclient is not None and hasattr(_real_testclient, "TestClient"):
    TestClient = _real_testclient.TestClient
else:

    class TestClient:
        __test__ = False

        def __init__(self, app: FastAPI) -> None:
            self._app = app

        def _request(self, method: str, path: str, *, json: object | None = None, headers: dict[str, str] | None = None):
            return self._app._handle(method, path, json=json, headers=headers)

        def get(self, path: str, *, headers: dict[str, str] | None = None):
            return self._request("GET", path, headers=headers)

        def post(self, path: str, *, json: object | None = None, headers: dict[str, str] | None = None):
            return self._request("POST", path, json=json, headers=headers)

        def patch(self, path: str, *, json: object | None = None, headers: dict[str, str] | None = None):
            return self._request("PATCH", path, json=json, headers=headers)

        def delete(self, path: str, *, headers: dict[str, str] | None = None):
            return self._request("DELETE", path, headers=headers)
