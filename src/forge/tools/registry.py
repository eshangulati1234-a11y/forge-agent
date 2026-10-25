"""Typed tool registry. Every tool declares pydantic input, side-effect level and capability tags.

Arguments are always validated against the declared model before policy or execution, which is how
hallucinated / malformed tool calls are stopped: the model sees a structured validation error instead.
"""
from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Awaitable, Callable

from pydantic import BaseModel, ValidationError


class SideEffect(StrEnum):
    NONE = "none"                # pure read / compute
    LOCAL_WRITE = "local_write"  # writes inside allowed paths / local DB
    EXTERNAL = "external"        # leaves the machine or acts as the user: always approval-gated


@dataclass
class ToolSpec:
    name: str
    description: str
    input_model: type[BaseModel]
    fn: Callable[..., Any]
    side_effect: SideEffect = SideEffect.NONE
    caps: frozenset[str] = frozenset()     # fs.read fs.write net desktop browser gpu records shell
    path_fields: tuple[str, ...] = ()      # arg fields policy checks against fs grants
    url_fields: tuple[str, ...] = ()       # arg fields policy checks against net allowlist
    resource: str | None = None            # exclusive resource lock, e.g. "desktop"
    mock: Callable[..., Any] | None = None # dry-run substitute for side-effecting tools
    source: str = "builtin"                # builtin | plugin:<name> | mcp:<server> | generated

    def json_schema(self) -> dict:
        schema = self.input_model.model_json_schema()
        schema.pop("title", None)
        return schema

    def openai_tool(self) -> dict:
        return {"type": "function", "function": {
            "name": self.name.replace(".", "__"),  # some chat templates reject dots
            "description": f"{self.description} [side_effect={self.side_effect}]",
            "parameters": self.json_schema()}}

    def validate(self, args: dict) -> BaseModel:
        return self.input_model.model_validate(args or {})

    async def call(self, args: BaseModel, ctx: "ToolContext") -> Any:
        fn = self.mock if (ctx.dry_run and self.side_effect != SideEffect.NONE and self.mock) else self.fn
        if ctx.dry_run and self.side_effect != SideEffect.NONE and not self.mock:
            return {"dry_run": True, "tool": self.name, "would_call_with": args.model_dump(mode="json")}
        if inspect.iscoroutinefunction(fn):
            return await fn(args, ctx)
        return await asyncio.to_thread(fn, args, ctx)


@dataclass
class ToolContext:
    """What a tool may touch at call time. Built by the runtime/agent, never by the model."""
    services: Any                      # forge.services.Services
    run_id: str | None = None
    session_id: str | None = None
    workflow_id: str | None = None
    step_path: str | None = None
    dry_run: bool = False
    extra: dict = field(default_factory=dict)

    def emit(self, kind: str, /, **data) -> None:
        self.services.events.emit(kind, run_id=self.run_id, session_id=self.session_id, step=self.step_path, **data)


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}
        self.disabled: list[str] = []   # globs from settings (tools.disabled)

    def enabled(self, name: str) -> bool:
        import fnmatch
        return not any(fnmatch.fnmatchcase(name, p) for p in self.disabled)

    def active(self) -> list[ToolSpec]:
        return [t for t in self.all() if self.enabled(t.name)]

    def register(self, spec: ToolSpec) -> ToolSpec:
        self._tools[spec.name] = spec
        return spec

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name) or self._tools.get(name.replace("__", "."))

    def all(self) -> list[ToolSpec]:
        return sorted(self._tools.values(), key=lambda t: t.name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def search(self, query: str, limit: int = 10) -> list[ToolSpec]:
        """Lexical tool discovery so the agent only sees relevant tools (keeps context small)."""
        words = {w for w in query.lower().replace(".", " ").split() if len(w) > 2}
        scored = []
        for t in self._tools.values():
            if not self.enabled(t.name):
                continue
            hay = f"{t.name} {t.description}".lower()
            s = sum(w in hay for w in words)
            if s:
                scored.append((s, t))
        return [t for _, t in sorted(scored, key=lambda x: -x[0])[:limit]]

    def catalog(self) -> list[dict]:
        return [{"name": t.name, "description": t.description, "side_effect": str(t.side_effect),
                 "caps": sorted(t.caps), "input_schema": t.json_schema()} for t in self.all()]


REGISTRY = ToolRegistry()


def tool(name: str, input_model: type[BaseModel], *, description: str = "", side_effect: SideEffect = SideEffect.NONE,
         caps: set[str] | None = None, path_fields: tuple[str, ...] = (), url_fields: tuple[str, ...] = (),
         resource: str | None = None, mock: Callable | None = None, registry: ToolRegistry | None = None,
         source: str = "builtin"):
    def deco(fn: Callable[[Any, ToolContext], Any | Awaitable[Any]]):
        (registry or REGISTRY).register(ToolSpec(
            name=name, description=description or (fn.__doc__ or "").strip().split("\n")[0],
            input_model=input_model, fn=fn, side_effect=side_effect, caps=frozenset(caps or ()),
            path_fields=path_fields, url_fields=url_fields, resource=resource, mock=mock, source=source))
        return fn
    return deco


def format_validation_error(e: ValidationError) -> list[dict]:
    return [{"field": ".".join(str(p) for p in err["loc"]), "error": err["msg"], "type": err["type"]}
            for err in e.errors()]
