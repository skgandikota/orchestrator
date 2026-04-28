"""Tool implementations callable by the coracle.

Built-in tools are imported eagerly. The MCP client (which surfaces
remote tools through :class:`~coracle.tools.mcp_client.MCPManager`)
is wired up lazily by callers that have a config path — see
``coracle.cli`` and ``docs/PLAN.md`` for the wiring.
"""

from . import _registrations  # noqa: F401  -- side-effect: populates default_registry
from ._sandbox import WorkspaceEscapeError, resolve_in_workspace
from .fs import delete_file, list_dir, read_file, write_file
from .registry import Registry, Tool, ToolResult, default_registry
from .shell import CommandResult, DeniedCommandError, run_command

__all__ = [
    "CommandResult",
    "DeniedCommandError",
    "Registry",
    "Tool",
    "ToolResult",
    "WorkspaceEscapeError",
    "default_registry",
    "delete_file",
    "list_dir",
    "read_file",
    "resolve_in_workspace",
    "run_command",
    "write_file",
]
