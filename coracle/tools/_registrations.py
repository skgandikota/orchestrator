"""Default-registry registrations for the Phase 4 tools.

This module wraps each Phase 4 tool's public functions in :class:`Tool`
records and registers them on :data:`default_registry`. Importing
:mod:`coracle.tools` runs this module, which is sufficient to populate
the registry for the coder model. Tool source files are not modified —
all hand-written OpenAI-compatible JSON Schemas live here.
"""

from __future__ import annotations

from typing import Any

from . import fs as fs_tool
from . import git as git_tool
from . import shell as shell_tool
from . import web as web_tool
from .registry import Tool, default_registry

__all__ = ["register_default_tools"]


def _schema(properties: dict[str, dict[str, Any]], required: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


_FS_TOOLS: list[Tool] = [
    Tool(
        name="fs.read_file",
        description="Read a UTF-8 text file from inside the workspace root.",
        parameters_schema=_schema(
            {"path": {"type": "string", "description": "Workspace-relative or absolute path."}},
            ["path"],
        ),
        fn=fs_tool.read_file,
        permissions={"fs_read": True},
    ),
    Tool(
        name="fs.write_file",
        description="Write UTF-8 content to a file inside the workspace root.",
        parameters_schema=_schema(
            {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            ["path", "content"],
        ),
        fn=fs_tool.write_file,
        permissions={"fs_write": True},
    ),
    Tool(
        name="fs.list_dir",
        description="List entries directly inside a workspace directory.",
        parameters_schema=_schema({"path": {"type": "string"}}, ["path"]),
        fn=fs_tool.list_dir,
        permissions={"fs_read": True},
    ),
    Tool(
        name="fs.delete_file",
        description="Delete a single file inside the workspace root.",
        parameters_schema=_schema({"path": {"type": "string"}}, ["path"]),
        fn=fs_tool.delete_file,
        permissions={"fs_write": True},
    ),
]


_SHELL_TOOLS: list[Tool] = [
    Tool(
        name="shell.run_command",
        description="Run an argv command inside the workspace, with allow/deny + timeout.",
        parameters_schema=_schema(
            {
                "cmd": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "description": "Argv list; shell=False is enforced.",
                },
                "timeout": {"type": "number", "exclusiveMinimum": 0, "default": 30},
                "cwd": {
                    "type": ["string", "null"],
                    "default": None,
                    "description": "Workspace-relative cwd; defaults to workspace root.",
                },
            },
            ["cmd"],
        ),
        fn=shell_tool.run_command,
        permissions={"shell": True},
    ),
]


_WEB_TOOLS: list[Tool] = [
    Tool(
        name="web.fetch",
        description="Fetch a URL via HTTP(S) and return sanitised text + metadata.",
        parameters_schema=_schema(
            {
                "url": {"type": "string", "format": "uri"},
                "max_bytes": {"type": "integer", "exclusiveMinimum": 0, "default": 1000000},
                "timeout": {"type": "number", "exclusiveMinimum": 0, "default": 15},
            },
            ["url"],
        ),
        fn=web_tool.fetch,
        permissions={"network": True},
    ),
    Tool(
        name="web.search",
        description="Run a web search via DuckDuckGo (default) or Brave.",
        parameters_schema=_schema(
            {
                "query": {"type": "string", "minLength": 1},
                "provider": {
                    "type": "string",
                    "enum": ["duckduckgo", "brave"],
                    "default": "duckduckgo",
                },
                "limit": {"type": "integer", "minimum": 1, "default": 10},
            },
            ["query"],
        ),
        fn=web_tool.search,
        permissions={"network": True},
    ),
]


_GIT_TOOLS: list[Tool] = [
    Tool(
        name="git.status",
        description="Return parsed `git status --porcelain=v2 --branch` for the workspace.",
        parameters_schema=_schema({}, []),
        fn=git_tool.status,
        permissions={"shell": True},
    ),
    Tool(
        name="git.diff",
        description="Return `git diff` (or `--cached` when staged=true) for the workspace.",
        parameters_schema=_schema(
            {
                "staged": {"type": "boolean", "default": False},
                "path": {"type": ["string", "null"], "default": None},
            },
            [],
        ),
        fn=git_tool.diff,
        permissions={"shell": True},
    ),
    Tool(
        name="git.commit",
        description="Create a commit and return the new SHA.",
        parameters_schema=_schema(
            {
                "message": {"type": "string", "minLength": 1},
                "add_all": {"type": "boolean", "default": False},
            },
            ["message"],
        ),
        fn=git_tool.commit,
        permissions={"shell": True},
    ),
    Tool(
        name="git.branch",
        description="Create a new git branch (no checkout).",
        parameters_schema=_schema({"name": {"type": "string", "minLength": 1}}, ["name"]),
        fn=git_tool.branch,
        permissions={"shell": True},
    ),
    Tool(
        name="git.checkout",
        description="Switch HEAD to ref, optionally creating it. Refuses dirty trees.",
        parameters_schema=_schema(
            {
                "ref": {"type": "string", "minLength": 1},
                "create": {"type": "boolean", "default": False},
            },
            ["ref"],
        ),
        fn=git_tool.checkout,
        permissions={"shell": True},
    ),
    Tool(
        name="git.log",
        description="Return the most recent N commits on HEAD.",
        parameters_schema=_schema({"n": {"type": "integer", "minimum": 0, "default": 10}}, []),
        fn=git_tool.log,
        permissions={"shell": True},
    ),
    Tool(
        name="git.current_branch",
        description="Return the symbolic name of the current branch.",
        parameters_schema=_schema({}, []),
        fn=git_tool.current_branch,
        permissions={"shell": True},
    ),
]


# Browser tools wrap a lazily-instantiated singleton so registration at
# import time does not spawn a Playwright worker. The wrapper closures
# instantiate on first call.
_browser_singleton: Any = None


def _get_browser() -> Any:
    global _browser_singleton
    if _browser_singleton is None:
        from .browser import BrowserTool

        _browser_singleton = BrowserTool()
    return _browser_singleton


def _browser_browse(url: str) -> Any:
    return _get_browser().browse(url)


def _browser_extract(url: str, selector: str) -> list[str]:
    return _get_browser().extract(url, selector)


def _browser_click(url: str, selector: str) -> Any:
    return _get_browser().click(url, selector)


def _browser_fill(url: str, selector: str, value: str) -> Any:
    return _get_browser().fill(url, selector, value)


_BROWSER_TOOLS: list[Tool] = [
    Tool(
        name="browser.browse",
        description="Navigate to URL and return a PageSnapshot (url/title/text/html/status).",
        parameters_schema=_schema({"url": {"type": "string", "format": "uri"}}, ["url"]),
        fn=_browser_browse,
        permissions={"network": True, "browser": True},
    ),
    Tool(
        name="browser.extract",
        description="Navigate to URL and return matched text for a CSS selector.",
        parameters_schema=_schema(
            {
                "url": {"type": "string", "format": "uri"},
                "selector": {"type": "string", "minLength": 1},
            },
            ["url", "selector"],
        ),
        fn=_browser_extract,
        permissions={"network": True, "browser": True},
    ),
    Tool(
        name="browser.click",
        description="Navigate to URL, click a CSS selector, return the post-click snapshot.",
        parameters_schema=_schema(
            {
                "url": {"type": "string", "format": "uri"},
                "selector": {"type": "string", "minLength": 1},
            },
            ["url", "selector"],
        ),
        fn=_browser_click,
        permissions={"network": True, "browser": True},
    ),
    Tool(
        name="browser.fill",
        description="Navigate to URL, fill a CSS selector with value, return the snapshot.",
        parameters_schema=_schema(
            {
                "url": {"type": "string", "format": "uri"},
                "selector": {"type": "string", "minLength": 1},
                "value": {"type": "string"},
            },
            ["url", "selector", "value"],
        ),
        fn=_browser_fill,
        permissions={"network": True, "browser": True},
    ),
]


_ALL_TOOLS: list[Tool] = [
    *_FS_TOOLS,
    *_SHELL_TOOLS,
    *_WEB_TOOLS,
    *_GIT_TOOLS,
    *_BROWSER_TOOLS,
]


def register_default_tools() -> None:
    """Register every Phase 4 tool on :data:`default_registry` (idempotent)."""
    for tool in _ALL_TOOLS:
        if default_registry.get(tool.name) is None:
            default_registry.register(tool)


register_default_tools()
