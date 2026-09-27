"""Expose the shared ``lab-tracker`` distribution version to the client package.

The client ships in the same distribution as the server, so its version is the
server's; ``lab_tracker._version`` owns the lookup.
"""

from __future__ import annotations

from lab_tracker._version import __version__

__all__ = ["__version__"]
