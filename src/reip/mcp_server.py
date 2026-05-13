"""MCP server — expose reip's analytical tools to any MCP-capable agent.

Idea borrowed from InsForge: instead of forcing every Anthropic call
through our /api/chat endpoint (which we pay for via the user's API key),
we expose the platform's 12 tools over MCP. Then Claude Code, Cursor,
Continue, etc. can invoke the tools directly — and the LLM cost runs on
THEIR quota (which the user already pays for via Claude Pro/Max), not on
the platform's API key.

Architecture:
  - Lower-level mcp.server.Server (NOT FastMCP — FastMCP auto-builds
    schemas from Python signatures, which fights our existing
    chat.TOOLS schema definitions). We want the chat.TOOLS schemas to
    stay authoritative.
  - stdio JSON-RPC transport (Anthropic's MCP standard).
  - Each chat tool from chat.TOOLS becomes an MCP tool with the same
    name + input_schema, no drift.
  - Tool execution reuses chat._execute() — single source of truth.
  - No LLM is invoked here — pure data dispatch, $0 API cost.

Run via:
  reip mcp

Wire into Claude Code (~/.claude.json):
  {
    "mcpServers": {
      "reip": {
        "command": "<absolute-path>/realestate-platform/.venv/bin/reip",
        "args": ["mcp"]
      }
    }
  }
"""
from __future__ import annotations
import asyncio
import json
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

from . import chat as chat_mod


def _build_server() -> Server:
    """Construct the MCP server with one MCP tool per chat tool."""
    srv: Server = Server("reip")

    @srv.list_tools()
    async def list_tools() -> list[Tool]:
        return [
            Tool(
                name=t["name"],
                description=t["description"],
                inputSchema=t["input_schema"],
            )
            for t in chat_mod.TOOLS
        ]

    @srv.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any] | None = None) -> list[TextContent]:
        """Dispatch to chat._execute(). Returns JSON-serialized result wrapped
        as MCP TextContent. Errors are returned as JSON, not raised, so the
        calling agent gets a structured response instead of a protocol fault."""
        try:
            result = chat_mod._execute(name, arguments or {})
            payload = json.dumps(result, default=str, indent=2)
        except Exception as e:
            payload = json.dumps({"error": f"{type(e).__name__}: {e}"})
        return [TextContent(type="text", text=payload)]

    return srv


async def _run_stdio_async() -> None:
    srv = _build_server()
    async with stdio_server() as (read_stream, write_stream):
        await srv.run(
            read_stream,
            write_stream,
            srv.create_initialization_options(),
        )


def run_stdio() -> None:
    """Block forever, serving JSON-RPC over stdin/stdout. The `reip mcp`
    CLI command calls this."""
    asyncio.run(_run_stdio_async())
