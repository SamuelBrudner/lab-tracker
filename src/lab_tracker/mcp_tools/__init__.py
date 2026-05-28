"""Grouped MCP tool modules for Lab Tracker."""

from lab_tracker.mcp_tools.read import READ_TOOLS, register_read_tools
from lab_tracker.mcp_tools.resources import register_resources
from lab_tracker.mcp_tools.write import WRITE_TOOLS, register_write_tools

__all__ = [
    "READ_TOOLS",
    "WRITE_TOOLS",
    "register_read_tools",
    "register_resources",
    "register_write_tools",
]
