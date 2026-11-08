"""forged: the persistent Forge runtime. Binds 127.0.0.1 only; clients (TUI/CLI, including over SSH from the Mac)
authenticate with the bearer token in FORGE_HOME/token. Closing any client never affects running automations.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
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
    import forge.browser.tools as bt
    if bt._MGR is not None:   # otherwise the Forge-profile browser windows outlive the daemon
        try:
            await asyncio.wait_for(bt._MGR.close(), 10)
        except Exception as e:
            log.warning("browser close failed: %s", e)
        bt._MGR = None


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
            "laya": {"enabled": S.laya is not None and S.cfg.laya.enabled, "loaded": bool(S.laya and S.laya.loaded),
                     "calls": S.laya.calls if S.laya else 0, "checkpoint": S.cfg.laya.checkpoint},
            "runs": {r["status"]: r["n"] for r in q}, "active_runs": list(S.runtime._tasks), "pending_approvals": len(S.approvals.pending()),
            "tools": len(S.registry.names()), "tools_enabled": len(S.registry.active()), "plugins": S.plugins,
            "pinned_models": S.router.pinned, "last_model_call": S.router.last_call, "uptime_s": time.time() - S.started_at,
            "schema_version": S.db.schema_version, "busy_sessions": [s for s, t in AGENT._running.items() if not t.done()],
            "restart_required": sorted(RESTART_PENDING)}


RESTART_PENDING: set[str] = set()


# ---------------------------------------------------------------- settings / tools / models / doctor / work
@app.get("/settings", dependencies=[Depends(auth)])
def get_settings():
    from forge import settings
    from forge.config import reload_config
    return {"settings": settings.as_dicts(reload_config()), "restart_required": sorted(RESTART_PENDING)}


class SettingBody(BaseModel):
    key: str
    value: Any
    confirm: bool = False


@app.post("/settings", dependencies=[Depends(auth)])
def set_setting(b: SettingBody):
    from forge import settings
    from forge.config import reload_config
    res = settings.set_value(b.key, b.value, confirm_danger=b.confirm)
    S.db.audit("user", "config.changed", {"key": b.key, "old": res["old"], "new": res["new"]})
    if res["apply"] == "live":
        settings.apply_live(S, reload_config())
    else:
        RESTART_PENDING.add(b.key)
    S.events.emit("config.changed", key=b.key, apply=res["apply"])
    return {**res, "applied": "live" if res["apply"] == "live" else "restart_required"}


@app.post("/settings/reload", dependencies=[Depends(auth)])
def reload_settings():
    from forge import settings
    from forge.config import reload_config
    settings.apply_live(S, reload_config())
    S.db.audit("user", "config.reloaded", {})
    return {"reloaded": True}


@app.get("/tools", dependencies=[Depends(auth)])
def tools():
    return [{"name": t.name, "description": t.description, "source": t.source, "side_effect": str(t.side_effect),
             "caps": sorted(t.caps), "enabled": S.registry.enabled(t.name)} for t in S.registry.all()]


class ToolToggle(BaseModel):
    pattern: str
    enabled: bool


@app.post("/tools/enable", dependencies=[Depends(auth)])
def tool_toggle(b: ToolToggle):
    disabled = [p for p in S.cfg.tools.disabled if p != b.pattern]
    if not b.enabled:
        disabled.append(b.pattern)
    return set_setting(SettingBody(key="tools.disabled", value=disabled))


@app.get("/models/available", dependencies=[Depends(auth)])
async def models_available():
    out = []
    for n in S.router.nodes.values():
        entry = {"node": n.cfg.name, "url": n.cfg.url, "healthy": n.available(), "models": []}
        if n.cfg.kind == "ollama":
            try:
                tags = (await S.router.client.get(f"{n.cfg.url}/api/tags", timeout=5)).json().get("models", [])
            except Exception:
                tags = []
            configured = {m.id: m for m in S.router.models if m.node == n.cfg.name}
            for t in tags:
                c = configured.get(t["name"])
                out_m = {"id": t["name"], "size_mb": int(t.get("size", 0) / 2**20), "resident": t["name"] in n.loaded,
                         "params": t.get("details", {}).get("parameter_size"), "quant": t.get("details", {}).get("quantization_level"),
                         "capabilities": c.capabilities if c else [], "configured": bool(c)}
                entry["models"].append(out_m)
        out.append(entry)
    caps = sorted({c for m in S.router.models for c in m.capabilities})
    return {"nodes": out, "capabilities": caps, "pins": S.router.pinned}


class PullBody(BaseModel):
    model: str
    node: str | None = None


@app.post("/models/pull", dependencies=[Depends(auth)])
async def models_pull(b: PullBody):
    node = S.router.nodes.get(b.node) if b.node else next(iter(S.router.nodes.values()))

    async def go():
        try:
            async with S.router.client.stream("POST", f"{node.cfg.url}/api/pull", json={"model": b.model, "stream": True}, timeout=None) as r:
                last = 0.0
                async for line in r.aiter_lines():
                    if not line:
                        continue
                    d = json.loads(line)
                    if time.time() - last > 0.5 or d.get("status") == "success" or "error" in d:
                        last = time.time()
                        S.events.emit("model.pull", model=b.model, node=node.cfg.name, status=d.get("status") or d.get("error"),
                                      completed=d.get("completed"), total=d.get("total"))
        except Exception as e:
            S.events.emit("model.pull", model=b.model, status=f"error: {e}")
    asyncio.create_task(go())
    return {"pulling": b.model, "node": node.cfg.name}


@app.get("/doctor", dependencies=[Depends(auth)])
async def doctor():
    from forge.setup.doctor import as_dicts, run_checks
    return as_dicts(await asyncio.to_thread(run_checks, S.cfg))


@app.get("/work", dependencies=[Depends(auth)])
def work():
    runs = []
    for rid in list(S.runtime._tasks):
        r = S.db.one("SELECT id,workflow_id,trigger,started_at FROM runs WHERE id=?", (rid,))
        step = S.db.one("SELECT step_path,kind,attempt,started_at FROM step_runs WHERE run_id=? AND status='running' ORDER BY started_at DESC LIMIT 1", (rid,))
        runs.append({**(r or {"id": rid}), "current_step": step})
    sessions = []
    for sid, t in AGENT._running.items():
        if not t.done():
            s = S.db.one("SELECT id,title,status FROM sessions WHERE id=?", (sid,))
            sessions.append(s or {"id": sid})
    try:
        from forge.desktop.tools import LEASE
        lease = LEASE.holder
    except Exception:
        lease = None
    calls = [e for e in S.events.buffer if e["kind"] == "model.call"][-12:]
    return {"runs": runs, "agent_turns": sessions, "desktop_lease": lease,
            "queued": S.db.one("SELECT COUNT(*) n FROM runs WHERE status='queued'")["n"],
            "waiting": S.db.all("SELECT id,workflow_id,status,error FROM runs WHERE status IN ('waiting_approval','waiting_user') ORDER BY created_at DESC LIMIT 20"),
            "model_calls": calls}


class TitleBody(BaseModel):
    title: str


@app.post("/sessions/{sid}/title", dependencies=[Depends(auth)])
def session_title(sid: str, b: TitleBody):
    S.db.execute("UPDATE sessions SET title=? WHERE id=?", (b.title[:80], sid))
    return {"ok": True}


@app.post("/shutdown", dependencies=[Depends(auth)])
async def shutdown():
    S.db.audit("user", "daemon.shutdown_requested", {})

    async def stop():
        await asyncio.sleep(0.3)
        if SERVER is not None:
            SERVER.should_exit = True
    asyncio.create_task(stop())
    return {"stopping": True}


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
    pins = dict(S.cfg.routing.pins)
    if b.model:
        pins[b.capability] = b.model
    else:
        pins.pop(b.capability, None)
    set_setting(SettingBody(key="routing.pins", value=pins))   # persisted + audited + applied live
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
async def interrupt(sid: str):
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


@app.post("/workflows/{wid}/disable", dependencies=[Depends(auth)])
def disable_wf(wid: str):
    return S.workflows.disable(wid)


@app.post("/workflows/{wid}/enable", dependencies=[Depends(auth)])
def enable_wf(wid: str):
    ap = S.workflows.enable(wid)
    return {"approval_id": ap["id"], "title": ap["title"]}


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


SERVER: Any = None


def serve() -> None:
    global SERVER
    import uvicorn
    from forge.config import forge_home, load_config
    cfg = load_config()
    log_path = forge_home() / "logs" / "forged.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [logging.FileHandler(log_path, encoding="utf-8")]
    import sys
    if sys.stderr is not None and "pythonw" not in sys.executable.lower():
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", handlers=handlers)
    SERVER = uvicorn.Server(uvicorn.Config(app, host=cfg.daemon.host, port=cfg.daemon.port, log_level="warning",
                                           log_config=None))
    SERVER.run()
    # The lifespan cleanup has run. Non-daemon threads (Playwright, torch, watchdog) used to keep the process alive
    # after /shutdown, so every `forge restart` left an old daemon behind (nine of them were found running).
    logging.getLogger("forge.daemon").info("forged stopped")
    logging.shutdown()
    os._exit(0)
