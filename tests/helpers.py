"""Test-only tools and workflow helpers."""
import json
import time
from pathlib import Path

from pydantic import BaseModel

from forge.tools.registry import SideEffect, tool


class Echo(BaseModel):
    value: object = None


@tool("test.echo", Echo, description="echo")
def echo(a, ctx):
    return {"value": a.value}


class Counter(BaseModel):
    path: str
    sleep_s: float = 0


@tool("test.count", Counter, description="append to a counter file (local write)", side_effect=SideEffect.LOCAL_WRITE,
      path_fields=("path",))
def count(a, ctx):
    p = Path(a.path)
    n = int(p.read_text()) + 1 if p.exists() else 1
    p.write_text(str(n))
    time.sleep(a.sleep_s)
    return {"n": n}


class Send(BaseModel):
    to: str
    body: str


@tool("test.send", Send, description="pretend to send a message", side_effect=SideEffect.EXTERNAL)
def send(a, ctx):
    return {"sent_to": a.to}


class Flaky(BaseModel):
    fail_times: int
    path: str


@tool("test.flaky", Flaky, description="fails transiently N times", path_fields=("path",))
def flaky(a, ctx):
    from forge.errors import TransientError
    p = Path(a.path)
    n = int(p.read_text()) + 1 if p.exists() else 1
    p.write_text(str(n))
    if n <= a.fail_times:
        raise TransientError(f"flake {n}")
    return {"attempts": n}


def deploy(services, ir_dict) -> int:
    from forge.workflows.ir import WorkflowIR
    ir = WorkflowIR.model_validate(ir_dict)
    v = services.workflows.create_version(ir, created_by="user")
    ap = services.workflows.request_deploy(v["id"], "test")
    row = services.approvals.decide(ap["id"], True)
    services.workflows.handle_approval(row)
    return v["id"]


def dumps(x):
    return json.dumps(x, default=str)
