"""Startup hook behind ``lt setup autotrack --scripts`` (stdlib only, never raises).

The ``lab_tracker_autotrack.pth`` file in site-packages runs one guarded line
at every interpreter start: unless ``LAB_TRACKER_AUTOTRACK`` is off it
compiles this file by path, without importing ``lab_tracker_client``, and
calls :func:`install`. That only adds :class:`MatplotlibWatcher` to
``sys.meta_path``. When ``matplotlib.figure`` or ``matplotlib.pyplot``
finishes importing in a process that is not IPython, the watcher swaps in
lazy stand-ins for ``Figure.savefig`` and ``pyplot.show``. The first call to
either restores the originals and asks ``lab_tracker_client.script_capture``
to install the real hooks, so a script that never saves or shows a figure
never imports Lab Tracker at all.
"""

from __future__ import annotations

import os
import sys

# Not ``typing.TYPE_CHECKING``: importing typing would cost every interpreter start.
TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Sequence
    from importlib.machinery import ModuleSpec
    from types import ModuleType

MODULE_NAME = "_lab_tracker_autotrack_pth"
AUTOTRACK_ENV = "LAB_TRACKER_AUTOTRACK"
FIGURE_MODULE = "matplotlib.figure"
PYPLOT_MODULE = "matplotlib.pyplot"
_LAZY: list[tuple[object, str, object, object]] = []


def enabled() -> bool:
    """False only when the kill switch is set (``0``, ``false``, ``no``, ``off``)."""

    return os.environ.get(AUTOTRACK_ENV, "1").strip().lower() not in {"0", "false", "no", "off"}


def install() -> bool:
    """Put the matplotlib watcher on ``sys.meta_path`` once; never raises."""

    try:
        if not enabled():
            return False
        for finder in sys.meta_path:
            if getattr(finder, "_lab_tracker_watcher", False):
                return True
        watcher = MatplotlibWatcher()
        sys.meta_path.insert(0, watcher)
        for name in (FIGURE_MODULE, PYPLOT_MODULE):
            module = sys.modules.get(name)
            if module is not None:
                watcher.loaded(name, module)
        return True
    except Exception:  # noqa: BLE001 - interpreter startup must never fail.
        return False


class MatplotlibWatcher:
    """Meta-path finder that notices matplotlib's figure and pyplot modules load."""

    _lab_tracker_watcher = True

    def __init__(self) -> None:
        self.pending = {FIGURE_MODULE, PYPLOT_MODULE}
        self._busy = False

    def find_spec(
        self,
        fullname: str,
        path: Sequence[str] | None = None,
        target: ModuleType | None = None,
    ) -> ModuleSpec | None:
        if fullname not in self.pending or self._busy:
            return None
        self._busy = True
        try:
            for finder in list(sys.meta_path):
                find_spec = getattr(finder, "find_spec", None)
                if finder is self or find_spec is None:
                    continue
                spec: ModuleSpec | None = find_spec(fullname, path, target)
                if spec is not None:
                    loader = getattr(spec, "loader", None)
                    if loader is None or not hasattr(loader, "exec_module"):
                        return None
                    spec.loader = _PostImportLoader(loader, fullname, self)  # type: ignore[assignment]
                    return spec
            return None
        except Exception:  # noqa: BLE001 - fall back to the ordinary import.
            return None
        finally:
            self._busy = False

    def loaded(self, name: str, module: ModuleType) -> None:
        """React to a finished import: lazy hooks, or step aside inside IPython."""

        self.pending.discard(name)
        try:
            active = enabled() and not _in_ipython()
            if not self.pending or not active:
                self.remove()
            if not active:
                return
            if name == FIGURE_MODULE:
                _lazy_patch(getattr(module, "Figure", None), "savefig")
            elif name == PYPLOT_MODULE:
                _lazy_patch(module, "show")
        except Exception:  # noqa: BLE001 - never break a matplotlib import.
            return

    def remove(self) -> None:
        try:
            sys.meta_path.remove(self)
        except ValueError:
            return


class _PostImportLoader:
    """Delegating loader that calls the watcher once the real module has run."""

    def __init__(self, loader: object, name: str, watcher: MatplotlibWatcher) -> None:
        self._loader = loader
        self._name = name
        self._watcher = watcher

    def create_module(self, spec: ModuleSpec) -> ModuleType | None:
        create = getattr(self._loader, "create_module", None)
        return create(spec) if create is not None else None

    def exec_module(self, module: ModuleType) -> None:
        try:
            self._loader.exec_module(module)  # type: ignore[attr-defined]
        finally:
            try:
                # Hand the module back to its real loader for introspection.
                module.__loader__ = self._loader  # type: ignore[assignment]
                spec = getattr(module, "__spec__", None)
                if spec is not None:
                    spec.loader = self._loader
            except Exception:  # noqa: BLE001 - cosmetic only.
                pass
        self._watcher.loaded(self._name, module)

    def __getattr__(self, name: str) -> object:
        return getattr(self._loader, name)


def _lazy_patch(owner: object, attribute: str) -> None:
    original = getattr(owner, attribute, None) if owner is not None else None
    if original is None or getattr(original, "_lab_tracker_lazy", False):
        return

    def lazy(*args: object, **kwargs: object) -> object:
        _activate()
        current = getattr(owner, attribute, original)
        # Dispatch to the hook now installed, unless this stand-in is itself
        # what that hook wraps (a hook installed on top of it): then run the
        # original, or the call would loop back here.
        if current is lazy or _wraps(current, lazy):
            current = original
        return current(*args, **kwargs)

    lazy._lab_tracker_lazy = True  # type: ignore[attr-defined]
    lazy.__wrapped__ = original  # type: ignore[attr-defined]
    lazy.__name__ = getattr(original, "__name__", attribute)
    lazy.__qualname__ = getattr(original, "__qualname__", attribute)
    lazy.__doc__ = getattr(original, "__doc__", None)
    setattr(owner, attribute, lazy)
    _LAZY.append((owner, attribute, original, lazy))


def _wraps(func: object, target: object) -> bool:
    for _depth in range(8):
        func = getattr(func, "__wrapped__", None)
        if func is None:
            return False
        if func is target:
            return True
    return False


def _activate() -> None:
    """First save or show: restore the originals, then install the real hooks.

    Runs once per batch of stand-ins, so a hook the user later removes (for
    example with ``autotrack(False)``) is never reinstalled behind their back.
    """

    if not _LAZY:
        return
    while _LAZY:
        owner, attribute, original, lazy = _LAZY.pop()
        try:
            if getattr(owner, attribute, None) is lazy:
                setattr(owner, attribute, original)
        except Exception:  # noqa: BLE001 - keep restoring the rest.
            continue
    if not enabled():
        return
    try:
        from lab_tracker_client import script_capture

        script_capture.activate_script_autotrack()
    except Exception:  # noqa: BLE001 - a missing or broken client never breaks plotting.
        return


def _in_ipython() -> bool:
    getter = getattr(sys.modules.get("IPython"), "get_ipython", None)
    try:
        return getter is not None and getter() is not None
    except Exception:  # noqa: BLE001 - no shell.
        return False
