"""Single choke point for every tool call (agent turns and workflow steps):
validate args -> policy -> resource lock -> execute with timeout -> record -> return.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

from pydantic import ValidationError

from forge.db import canonical, new_id, now, sha
from forge.errors import ForgeError, InvalidInput, classify
from forge.policy.engine import Permissions
from forge.tools.registry import ToolContext, format_validation_error


async def invoke_tool(services, name: str, raw_args: dict, perms: Permissions, ctx: ToolContext, *,
                      interactive: bool, timeout_s: float = 300, scope: dict | None = None) -> Any:
    spec = services.registry.get(name)
    call_id = new_id("tc_")
    started = time.time()
    if spec is None:
        raise InvalidInput(f"unknown tool '{name}'", suggestions=[t.name for t in services.registry.search(name, 5)])
    try:
        args = spec.validate(raw_args)
    except ValidationError as e:
        errs = format_validation_error(e)
        _record(services, call_id, ctx, spec.name, raw_args, "error", None, f"validation: {errs}", "invalid_input", started)
        raise InvalidInput(f"invalid arguments for {spec.name}", errors=errs, schema=spec.json_schema()) from None

    scope = scope or ({"run_id": ctx.run_id, "workflow_id": ctx.workflow_id, "step": ctx.step_path} if ctx.run_id
                      else {"session_id": ctx.session_id})
    try:
        services.policy.check(spec, args, perms, scope=scope, interactive=interactive, dry_run=ctx.dry_run)
    except ForgeError as e:
        _record(services, call_id, ctx, spec.name, raw_args, "denied" if e.error_class == "permission_denied" else "needs_approval",
                None, str(e), str(e.error_class), started)
        raise

    lock = services.resources.get(spec.resource) if spec.resource else None
    ctx.emit("tool.start", tool=spec.name, call_id=call_id, args=_preview(args.model_dump(mode="json")))
    try:
        if lock is not None:
            async with lock:
                result = await asyncio.wait_for(spec.call(args, ctx), timeout_s)
        else:
            result = await asyncio.wait_for(spec.call(args, ctx), timeout_s)
    except Exception as e:
        cls = classify(e)
        _record(services, call_id, ctx, spec.name, raw_args, "error", None, f"{type(e).__name__}: {e}", str(cls), started)
        ctx.emit("tool.error", tool=spec.name, call_id=call_id, error=str(e)[:300], error_class=str(cls))
        raise
    _record(services, call_id, ctx, spec.name, raw_args, "ok", result, None, None, started)
    ctx.emit("tool.end", tool=spec.name, call_id=call_id, ms=int((time.time() - started) * 1000), result=_preview(result))
    return result


def _preview(obj: Any, n: int = 400) -> str:
    s = canonical(obj)
    return s if len(s) <= n else s[:n] + "…"


def _record(services, call_id, ctx, tool, args, status, result, error, error_class, started) -> None:
    services.db.execute(
        "INSERT INTO tool_calls(id,session_id,run_id,step_path,tool,args_json,args_hash,status,result_json,error,error_class,duration_ms,created_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (call_id, ctx.session_id, ctx.run_id, ctx.step_path, tool, canonical(args), sha({"t": tool, "a": args}), status,
         canonical(result)[:200_000] if result is not None else None, error, error_class,
         int((time.time() - started) * 1000), now()))
