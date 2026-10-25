"""forged: the persistent Forge runtime. Binds 127.0.0.1 only; clients (TUI/CLI, including over SSH from the Mac)
authenticate with the bearer token in FORGE_HOME/token. Closing any client never affects running automations.
"""
from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from forge.config import api_token
from forge.errors import ForgeError

log = logging.getLogger("forge.daemon")
S: Any = None       # Services
AGENT: Any = None
GPU: dict = {}


def auth(authorization: str = Header("")) -> None:
    if authorization != f"Bearer {api_token()}":
        raise HTTPException(401, "bad token")


async def _gpu_loop() -> None:
    while True:
        try:
            out = await asyncio.to_thread(subprocess.run, ["nvidia-smi", "--query-gpu=name,memory.used,memory.total,utilization.gpu,temperature.gpu",
                                                            "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=5,
                                          creationflags=0x08000000)
            name, used, total, util, temp = [x.strip() for x in out.stdout.strip().split(",")]
            GPU.update(name=name, mem_used_mb=int(used), mem_total_mb=int(total), util_pct=int(util), temp_c=int(temp), ts=time.time())
        except Exception as e:
            GPU.update(error=str(e))
        await asyncio.sleep(5)


async def _router_loop() -> None:
    while True:
        await S.router.refresh()
        await asyncio.sleep(15)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global S, AGENT
    from forge.agent.loop import Agent
    from forge.services import Services
    S = Services()
    S.events.bind_loop(asyncio.get_running_loop())
    S.load_builtin_tools()
    S.load_plugins()
    AGENT = Agent(S)
    from forge.tools.mcp_bridge import McpBridge
    from forge.tools.toolgen import load_generated
    bridge = McpBridge(S)
    mcp_tools = await bridge.start() if S.cfg.mcp_servers else []
    gen_tools = load_generated(S)
    # sessions that were mid-turn when the daemon died are marked interrupted (user can continue them)
    S.db.execute("UPDATE sessions SET status='interrupted' WHERE status='running'")
    S.db.audit("daemon", "daemon.start", {"tools": len(S.registry.names()), "plugins": S.plugins})
    tasks = [asyncio.create_task(c) for c in (S.runtime.serve(), S.scheduler.serve(), _router_loop(), _gpu_loop())]
    if S.laya is not None:
        asyncio.create_task(asyncio.to_thread(S.laya._ensure))   # warm Laya in the background (~40 s cold)
    log.info("forged ready: %d tools (mcp %d, generated %d), plugins=%s", len(S.registry.names()), len(mcp_tools), len(gen_tools), S.plugins)
    yield
    await bridge.aclose()
    await S.runtime.stop()
    for t in tasks:
        t.cancel()
    S.db.audit("daemon", "daemon.stop", {})
    await S.router.aclose()


app = FastAPI(title="forged", lifespan=lifespan)


@app.exception_handler(ForgeError)
async def forge_error(_: Request, e: ForgeError):
    from fastapi.responses import JSONResponse
    return JSONResponse(status_code=400, content={"error": str(e), "error_class": str(e.error_class), "details": e.details})


# ---------------------------------------------------------------- status
@app.get("/status", dependencies=[Depends(auth)])
def status():
    q = S.db.all("SELECT status, COUNT(*) n FROM runs GROUP BY status")
    return {"ok": True, "emergency_stop": S.policy.stopped(), "nodes": S.router.status(), "gpu": GPU,
            "laya": {"enabled": S.laya is not None, "loaded": bool(S.laya and S.laya.loaded), "calls": S.laya.calls if S.laya else 0,
                     "checkpoint": S.cfg.laya.checkpoint},
            "runs": {r["status"]: r["n"] for r in q}, "active_runs": list(S.runtime._tasks), "pending_approvals": len(S.approvals.pending()),
            "tools": len(S.registry.names()), "plugins": S.plugins, "pinned_models": S.router.pinned}


@app.post("/stop", dependencies=[Depends(auth)])
def stop():
    S.policy.set_stop(True)
    for rid in list(S.runtime._tasks):
        S.runtime._tasks[rid].cancel()
    S.events.emit("emergency_stop", on=True)
    return {"stopped": True}


@app.post("/resume", dependencies=[Depends(auth)])
def resume():
    S.policy.set_stop(False)
    S.db.execute("UPDATE runs SET status='queued' WHERE status='cancelled' AND error='cancelled' AND finished_at>?", (time.time() - 3600,))
    S.events.emit("emergency_stop", on=False)
    return {"stopped": False}


class PinBody(BaseModel):
    capability: str
    model: str | None = None


@app.post("/models/pin", dependencies=[Depends(auth)])
def pin(b: PinBody):
    if b.model:
        S.router.pinned[b.capability] = b.model
    else:
        S.router.pinned.pop(b.capability, None)
    return S.router.pinned


@app.get("/models", dependencies=[Depends(auth)])
def models():
    return [m.model_dump() for m in S.router.models]


# ---------------------------------------------------------------- chat
@app.post("/sessions", dependencies=[Depends(auth)])
def new_session():
    return {"session_id": AGENT.new_session()}


@app.get("/sessions", dependencies=[Depends(auth)])
def sessions():
    return AGENT.sessions()


@app.get("/sessions/{sid}/messages", dependencies=[Depends(auth)])
def messages(sid: str, limit: int = 100):
    return AGENT.history(sid, limit)


class ChatBody(BaseModel):
    text: str


@app.post("/sessions/{sid}/chat", dependencies=[Depends(auth)])
async def chat(sid: str, b: ChatBody):
    if sid in AGENT._running and not AGENT._running[sid].done():
        raise HTTPException(409, "session is busy; interrupt it first")
    task = asyncio.create_task(AGENT.run_turn(sid, b.text))
    AGENT._running[sid] = task
    return {"accepted": True}


@app.post("/sessions/{sid}/interrupt", dependencies=[Depends(auth)])
def interrupt(sid: str):
    AGENT.interrupt(sid)
    return {"interrupted": True}


# ---------------------------------------------------------------- workflows
class CompileBody(BaseModel):
    request: str


@app.post("/workflows/compile", dependencies=[Depends(auth)])
async def compile_wf(b: CompileBody):
    from forge.workflows.compiler import Compiler
    return (await Compiler(S).compile(b.request, requested_by="user")).summary()


class ImportBody(BaseModel):
    yaml: str
    request_deploy: bool = True


@app.post("/workflows/import", dependencies=[Depends(auth)])
async def import_wf(b: ImportBody):
    from forge.workflows.ir import WorkflowIR
    ir = WorkflowIR.from_yaml(b.yaml)
    v = S.workflows.create_version(ir, created_by="user")
    tests = await S.workflows.run_tests(v["id"])
    out: dict[str, Any] = {"workflow_id": ir.id, "version_id": v["id"], "version": v["version"], "tests": tests}
    if b.request_deploy and all(t["passed"] for t in tests):
        out["approval_id"] = S.workflows.request_deploy(v["id"], "user", {"tests": tests})["id"]
    return out


@app.get("/workflows", dependencies=[Depends(auth)])
def workflows():
    return S.workflows.list()


@app.get("/workflows/{wid}", dependencies=[Depends(auth)])
def workflow(wid: str):
    vs = S.workflows.versions(wid)
    dep = S.workflows.deployed(wid)
    ir = S.workflows.ir(dep["id"] if dep else vs[0]["id"]) if vs else None
    sched = S.db.all("SELECT * FROM schedules WHERE workflow_id=?", (wid,))
    return {"versions": vs, "yaml": ir.to_yaml() if ir else "", "schedules": sched}


class RunBody(BaseModel):
    inputs: dict = {}


@app.post("/workflows/{wid}/run", dependencies=[Depends(auth)])
def run_wf(wid: str, b: RunBody):
    return {"run_id": S.runtime.enqueue(wid, trigger="manual", inputs=b.inputs, priority=3)}


@app.post("/workflows/{wid}/rollback", dependencies=[Depends(auth)])
def rollback(wid: str, version: int | None = None):
    return S.workflows.rollback(wid, version)


@app.post("/hooks/{wid}")
async def webhook(wid: str, request: Request, x_forge_hook_token: str = Header("")):
    import hmac
    expected = S.db.get_kv(f"hook_token:{wid}")
    if not expected or not hmac.compare_digest(expected, x_forge_hook_token):
        raise HTTPException(401, "bad hook token")
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    return {"run_id": S.runtime.enqueue(wid, trigger="webhook", inputs={"payload": body})}


# ---------------------------------------------------------------- runs
@app.get("/runs", dependencies=[Depends(auth)])
def runs(limit: int = 50, workflow_id: str | None = None):
    if workflow_id:
        return S.db.all("SELECT * FROM runs WHERE workflow_id=? ORDER BY created_at DESC LIMIT ?", (workflow_id, limit))
    return S.db.all("SELECT * FROM runs WHERE trigger NOT IN ('test','dry_run') ORDER BY created_at DESC LIMIT ?", (limit,))


@app.get("/runs/{rid}", dependencies=[Depends(auth)])
def run(rid: str):
    return {"run": S.db.one("SELECT * FROM runs WHERE id=?", (rid,)),
            "steps": S.db.all("SELECT * FROM step_runs WHERE run_id=? ORDER BY started_at", (rid,)),
            "tool_calls": S.db.all("SELECT id,tool,status,error,duration_ms,created_at FROM tool_calls WHERE run_id=? ORDER BY created_at", (rid,))}


@app.post("/runs/{rid}/cancel", dependencies=[Depends(auth)])
def cancel(rid: str):
    S.runtime.cancel(rid)
    return {"cancelled": True}


@app.post("/runs/{rid}/resume", dependencies=[Depends(auth)])
def resume_run(rid: str, step_done: bool = False):
    S.runtime.resume_user(rid, step_done)
    return {"resumed": True}


# ---------------------------------------------------------------- approvals
@app.get("/approvals", dependencies=[Depends(auth)])
def approvals(status: str = "pending"):
    if status == "pending":
        return S.approvals.pending()
    return S.db.all("SELECT * FROM approvals ORDER BY requested_at DESC LIMIT 100")


class DecideBody(BaseModel):
    approve: bool
    reason: str = ""


@app.post("/approvals/{aid}", dependencies=[Depends(auth)])
def decide(aid: str, b: DecideBody):
    row = S.approvals.decide(aid, b.approve, decided_by="user", reason=b.reason)
    result = S.workflows.handle_approval(row)
    return {"approval": {k: row[k] for k in ("id", "status", "title")}, "result": result}


# ---------------------------------------------------------------- records / memory / logs
@app.get("/records", dependencies=[Depends(auth)])
def collections():
    return [{"name": c.name, "title": c.title, "count": len(S.records.list(c.name)),
             "conflicts": S.db.one("SELECT COUNT(*) n FROM record_conflicts WHERE collection=? AND resolved_at IS NULL", (c.name,))["n"]}
            for c in S.records.collections.values()]


@app.get("/records/{collection}", dependencies=[Depends(auth)])
def records(collection: str, limit: int = 200):
    spec = S.records.spec(collection)
    return {"fields": [f.name for f in spec.fields], "records": S.records.list(collection, limit=limit)}


class ExportBody(BaseModel):
    collections: list[str]
    path: str


@app.post("/records/export", dependencies=[Depends(auth)])
def export(b: ExportBody):
    from forge.records.excel import export_collections
    return export_collections(S.records, b.collections, Path(b.path))


@app.get("/memory", dependencies=[Depends(auth)])
async def memory(q: str = ""):
    return await S.memory.search(q, 20) if q else S.memory.list()


class RememberBody(BaseModel):
    content: str
    kind: str = "fact"


@app.post("/memory", dependencies=[Depends(auth)])
async def remember(b: RememberBody):
    return await S.memory.add(b.kind, b.content, "user", {"source": "tui"})   # user-stated: a FACT


@app.get("/events", dependencies=[Depends(auth)])
def events(since: int = 0):
    return S.events.since(since)


@app.get("/audit", dependencies=[Depends(auth)])
def audit(limit: int = 200):
    ok, bad = S.db.verify_audit_chain()
    return {"chain_ok": ok, "first_bad_id": bad, "entries": S.db.all("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,))}


@app.websocket("/ws")
async def ws(sock: WebSocket, token: str = Query("")):
    if token != api_token():
        await sock.close(code=4401)
        return
    await sock.accept()
    q = S.events.subscribe()
    try:
        for ev in list(S.events.buffer)[-50:]:
            await sock.send_text(json.dumps(ev, default=str))
        while True:
            ev = await q.get()
            await sock.send_text(json.dumps(ev, default=str))
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        S.events.unsubscribe(q)


def serve() -> None:
    import uvicorn
    from forge.config import forge_home, load_config
    cfg = load_config()
    log_path = forge_home() / "logs" / "forged.log"
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=[logging.FileHandler(log_path, encoding="utf-8"), logging.StreamHandler()])
    uvicorn.run(app, host=cfg.daemon.host, port=cfg.daemon.port, log_level="warning")
