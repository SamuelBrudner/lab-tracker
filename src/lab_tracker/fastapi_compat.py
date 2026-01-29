"""FastAPI compatibility layer with a local fallback."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

try:  # pragma: no cover - exercised when FastAPI is installed.
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
except ModuleNotFoundError:  # pragma: no cover - fallback for offline environments.
    import inspect
    from enum import Enum
    from urllib.parse import parse_qs, urlsplit
    from uuid import UUID

    @dataclass
    class _Response:
        status_code: int
        payload: Any

        def json(self) -> Any:
            return self.payload

    @dataclass
    class _Request:
        headers: dict[str, str]

    @dataclass
    class _Route:
        method: str
        path: str
        handler: Callable[..., Any]
        status_code: int

    def _split_path(path: str) -> list[str]:
        cleaned = path.strip("/")
        if not cleaned:
            return []
        return cleaned.split("/")

    def _match_path(template: str, actual: str) -> dict[str, str] | None:
        template_parts = _split_path(template)
        actual_parts = _split_path(actual)
        if len(template_parts) != len(actual_parts):
            return None
        params: dict[str, str] = {}
        for template_part, actual_part in zip(template_parts, actual_parts):
            if template_part.startswith("{") and template_part.endswith("}"):
                params[template_part[1:-1]] = actual_part
                continue
            if template_part != actual_part:
                return None
        return params

    def _unwrap_optional(annotation: Any) -> Any:
        args = getattr(annotation, "__args__", None)
        if args and type(None) in args and len(args) == 2:
            for arg in args:
                if arg is not type(None):
                    return arg
        return annotation

    def _coerce_value(value: str, annotation: Any) -> Any:
        if annotation is inspect._empty:
            return value
        if annotation is str:
            return value
        if annotation is int:
            return int(value)
        if annotation is float:
            return float(value)
        if annotation is bool:
            return value.lower() in {"true", "1", "yes", "y"}
        if annotation is UUID:
            return UUID(value)
        if isinstance(annotation, type) and issubclass(annotation, Enum):
            return annotation(value)
        return value

    def _build_body(value: Any, annotation: Any) -> Any:
        resolved = _unwrap_optional(annotation)
        if resolved is inspect._empty:
            return value
        if hasattr(resolved, "model_validate"):
            return resolved.model_validate(value)
        return value

    def _coerce_response(result: Any, status_code: int) -> _Response:
        if isinstance(result, _Response):
            return result
        if hasattr(result, "status_code") and hasattr(result, "json"):
            return result  # type: ignore[return-value]
        payload = result
        if hasattr(result, "model_dump"):
            payload = result.model_dump()
        elif hasattr(result, "dict"):
            payload = result.dict()
        return _Response(status_code=status_code, payload=payload)

    class FastAPI:
        def __init__(self, title: str | None = None) -> None:
            self.title = title or ""
            self._routes: list[_Route] = []
            self._exception_handlers: dict[type[Exception], Callable[..., Any]] = {}

        def _route(self, method: str, path: str, *, status_code: int) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
            def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
                self._routes.append(_Route(method=method, path=path, handler=func, status_code=status_code))
                return func

            return decorator

        def get(self, path: str, **kwargs: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
            return self._route("GET", path, status_code=kwargs.get("status_code", 200))

        def post(self, path: str, **kwargs: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
            return self._route("POST", path, status_code=kwargs.get("status_code", 200))

        def patch(self, path: str, **kwargs: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
            return self._route("PATCH", path, status_code=kwargs.get("status_code", 200))

        def delete(self, path: str, **kwargs: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
            return self._route("DELETE", path, status_code=kwargs.get("status_code", 200))

        def exception_handler(self, exc_type: type[Exception]) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
            def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
                self._exception_handlers[exc_type] = func
                return func

            return decorator

        def _find_route(self, method: str, path: str) -> tuple[_Route | None, dict[str, str]]:
            for route in self._routes:
                if route.method != method:
                    continue
                params = _match_path(route.path, path)
                if params is None:
                    continue
                return route, params
            return None, {}

        def _handle(
            self,
            method: str,
            raw_path: str,
            *,
            json: Any | None = None,
            headers: dict[str, str] | None = None,
        ) -> _Response:
            parsed = urlsplit(raw_path)
            path = parsed.path or "/"
            query_params = {key: values[-1] for key, values in parse_qs(parsed.query).items()}
            route, path_params = self._find_route(method, path)
            if route is None:
                return _Response(status_code=404, payload={"detail": "Not Found"})
            request = _Request(headers=headers or {})
            try:
                sig = inspect.signature(route.handler)
                kwargs: dict[str, Any] = {}
                body_consumed = False
                for name, param in sig.parameters.items():
                    if name in path_params:
                        kwargs[name] = _coerce_value(path_params[name], param.annotation)
                        continue
                    if name in query_params:
                        kwargs[name] = _coerce_value(query_params[name], param.annotation)
                        continue
                    if param.annotation is _Request or name == "request":
                        kwargs[name] = request
                        continue
                    if not body_consumed and json is not None:
                        kwargs[name] = _build_body(json, param.annotation)
                        body_consumed = True
                        continue
                    if param.default is not inspect._empty:
                        kwargs[name] = param.default
                result = route.handler(**kwargs)
            except Exception as exc:
                for exc_type, handler in self._exception_handlers.items():
                    if isinstance(exc, exc_type):
                        result = handler(request, exc)
                        return _coerce_response(result, getattr(result, "status_code", 500))
                raise
            return _coerce_response(result, route.status_code)

    class TestClient:
        __test__ = False

        def __init__(self, app: FastAPI) -> None:
            self._app = app

        def _request(
            self,
            method: str,
            path: str,
            *,
            json: object | None = None,
            headers: dict[str, str] | None = None,
        ):
            return self._app._handle(method, path, json=json, headers=headers)

        def get(self, path: str, *, headers: dict[str, str] | None = None):
            return self._request("GET", path, headers=headers)

        def post(self, path: str, *, json: object | None = None, headers: dict[str, str] | None = None):
            return self._request("POST", path, json=json, headers=headers)

        def patch(self, path: str, *, json: object | None = None, headers: dict[str, str] | None = None):
            return self._request("PATCH", path, json=json, headers=headers)

        def delete(self, path: str, *, headers: dict[str, str] | None = None):
            return self._request("DELETE", path, headers=headers)
