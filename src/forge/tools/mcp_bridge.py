"""MCP client bridge: tools from configured MCP servers become Forge tools (`mcp.<server>.<tool>`).

Each MCP tool gets a dynamic pydantic input model that enforces the server's JSON schema, so MCP calls go through
the same validate -> policy -> execute path as built-ins. MCP servers are third-party code: their tools default to
side_effect=external (approval-gated) unless forge.toml marks the server otherwise.
"""
from __future__ import annotations

import logging
from contextlib import AsyncExitStack
from typing import Any

import jsonschema
from pydantic import BaseModel, ConfigDict, model_validator

from forge.tools.registry import SideEffect, ToolSpec

log = logging.getLogger("forge.mcp")


def _attr(obj: Any, *names: str) -> Any:
    """mcp 1.x used camelCase fields (inputSchema, isError); mcp 2.x uses snake_case."""
    for n in names:
        v = getattr(obj, n, None)
        if v is not None:
            return v
    return None


def _model_for(schema: dict, name: str) -> type[BaseModel]:
    schema = schema or {"type": "object", "properties": {}}

    class McpArgs(BaseModel):
        model_config = ConfigDict(extra="allow")

        @model_validator(mode="before")
        @classmethod
        def _check(cls, data: Any) -> Any:
            try:
                jsonschema.validate(data or {}, schema)
            except jsonschema.ValidationError as e:   # surface as a pydantic error -> structured InvalidInput
                raise ValueError(f"{'/'.join(map(str, e.absolute_path)) or 'arguments'}: {e.message}") from None
            return data

        @classmethod
        def model_json_schema(cls, *a, **kw) -> dict:  # expose the server's own schema to models/compiler
            return dict(schema)

    McpArgs.__name__ = f"Mcp_{name}"
    return McpArgs


class McpBridge:
    def __init__(self, services):
        self.s = services
        self.stack = AsyncExitStack()
        self.sessions: dict[str, Any] = {}

    async def start(self) -> list[str]:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        added = []
        for srv in self.s.cfg.mcp_servers:
            try:
                read, write = await self.stack.enter_async_context(stdio_client(StdioServerParameters(command=srv.command, args=srv.args)))
                session = await self.stack.enter_async_context(ClientSession(read, write))
                await session.initialize()
                self.sessions[srv.name] = session
                for t in (await session.list_tools()).tools:
                    schema = _attr(t, "input_schema", "inputSchema") or {}
                    added.append(self.register(srv.name, t.name, t.description or "", schema, srv.side_effect))
            except Exception as e:
                log.warning("MCP server %s failed to start: %s", srv.name, e)
        return added

    def register(self, server: str, tool_name: str, description: str, schema: dict, side_effect: str) -> str:
        full = f"mcp.{server}.{tool_name}"

        async def call(args: BaseModel, ctx) -> Any:
            res = await self.sessions[server].call_tool(tool_name, args.model_dump())
            content = [getattr(c, "text", None) or getattr(c, "data", None) or str(c) for c in getattr(res, "content", []) or []]
            if _attr(res, "is_error", "isError"):
                from forge.errors import ForgeError
                raise ForgeError(f"MCP tool {full} error: {' '.join(map(str, content))[:500]}")
            out = {"content": content}
            structured = _attr(res, "structured_content", "structuredContent")
            if structured:
                out["structured"] = structured
            return out

        self.s.registry.register(ToolSpec(name=full, description=f"[MCP:{server}] {description}"[:500], input_model=_model_for(schema, full),
                                          fn=call, side_effect=SideEffect(side_effect), caps=frozenset({"mcp"}), source=f"mcp:{server}"))
        return full

    async def aclose(self) -> None:
        await self.stack.aclose()
