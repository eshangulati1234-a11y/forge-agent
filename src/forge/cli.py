"""forge CLI. Every command except `daemon`, `init` and `install-startup` is a client of the running daemon."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import typer

app = typer.Typer(no_args_is_help=True, add_completion=False, help="Forge: describe an automation once; Forge builds, verifies, deploys and keeps it running.")
wf = typer.Typer(help="workflows")
app.add_typer(wf, name="workflow")
TASK_NAME = "Forge Daemon"


def _c():
    from forge.client import SyncClient
    return SyncClient()


def _print(obj) -> None:
    typer.echo(json.dumps(obj, indent=2, default=str))


@app.command()
def init():
    """Create ~/.forge (config, token, dirs)."""
    from forge.config import init_home
    typer.echo(f"Forge home: {init_home()}")


def _ensure_ollama() -> None:
    import httpx
    try:
        httpx.get("http://127.0.0.1:11434/api/version", timeout=2)
        return
    except httpx.HTTPError:
        pass
    exe = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama.exe"
    if not exe.exists():
        exe = Path(shutil.which("ollama") or "")
    if exe.exists():
        env = {**os.environ, "OLLAMA_MAX_LOADED_MODELS": "1", "OLLAMA_NUM_PARALLEL": "1", "OLLAMA_FLASH_ATTENTION": "1",
               "OLLAMA_KV_CACHE_TYPE": "q8_0", "OLLAMA_HOST": "127.0.0.1:11434"}
        subprocess.Popen([str(exe), "serve"], env=env, creationflags=0x08000000 | 0x00000008,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(3)


@app.command()
def daemon(ensure_ollama: bool = typer.Option(True, help="start `ollama serve` if it is not running")):
    """Run the persistent Forge runtime (foreground)."""
    if ensure_ollama:
        _ensure_ollama()
    from forge.daemon.app import serve
    serve()


@app.command()
def tui():
    """Open the terminal UI (works over SSH)."""
    from forge.tui.app import run
    run()


@app.command()
def status():
    _print(_c().get("/status"))


@app.command()
def chat(text: str):
    """One-shot chat turn; streams agent events to stdout."""
    c = _c()
    sid = c.post("/sessions")["session_id"]
    seq = max([e["seq"] for e in c.get("/events")] or [0])
    c.post(f"/sessions/{sid}/chat", {"text": text})
    while True:
        for e in c.get("/events", since=seq):
            seq = e["seq"]
            if e.get("session_id") != sid:
                continue
            k = e["kind"]
            if k == "agent.delta":
                typer.echo(e["text"], nl=False)
            elif k == "tool.start":
                typer.secho(f"\n→ {e['tool']} {e.get('args', '')[:160]}", fg="cyan")
            elif k == "tool.error":
                typer.secho(f"  ✗ {e['error'][:200]}", fg="red")
            elif k == "agent.waiting_approval":
                typer.secho(f"\n⚠ approval needed: {e['reason']}  (forge approve {e['approval_id']})", fg="yellow")
            elif k == "agent.done":
                typer.echo(f"\n[{e['steps']} steps, {e['tokens']} tokens]")
                return
        time.sleep(0.3)


@app.command()
def compile(request: str):
    """Compile a natural-language automation into a tested workflow and request deploy approval."""
    res = _c().post("/workflows/compile", {"request": request})
    if res.get("yaml"):
        typer.echo(res["yaml"])
    _print({k: v for k, v in res.items() if k != "yaml"})


@wf.command("import")
def wf_import(path: Path, no_deploy: bool = False):
    _print(_c().post("/workflows/import", {"yaml": path.read_text(encoding="utf-8"), "request_deploy": not no_deploy}))


@wf.command("list")
def wf_list():
    _print(_c().get("/workflows"))


@wf.command("show")
def wf_show(workflow_id: str):
    d = _c().get(f"/workflows/{workflow_id}")
    typer.echo(d["yaml"])
    _print({"versions": d["versions"], "schedules": d["schedules"]})


@wf.command("run")
def wf_run(workflow_id: str, inputs: str = "{}", wait: bool = True):
    c = _c()
    rid = c.post(f"/workflows/{workflow_id}/run", {"inputs": json.loads(inputs)})["run_id"]
    typer.echo(f"queued {rid}")
    while wait:
        d = c.get(f"/runs/{rid}")
        if d["run"]["status"] not in ("queued", "running"):
            _print({"run": d["run"], "steps": [{k: s[k] for k in ("step_path", "status", "error_class", "error")} for s in d["steps"]]})
            return
        time.sleep(1)


@wf.command("rollback")
def wf_rollback(workflow_id: str, version: int | None = None):
    _print(_c().post(f"/workflows/{workflow_id}/rollback", version=version))


@app.command()
def approvals():
    for a in _c().get("/approvals"):
        typer.echo(f"{a['id']}  [{a['kind']}]  {a['title']}")


@app.command()
def approve(approval_id: str, reason: str = ""):
    _print(_c().post(f"/approvals/{approval_id}", {"approve": True, "reason": reason}))


@app.command()
def deny(approval_id: str, reason: str = ""):
    _print(_c().post(f"/approvals/{approval_id}", {"approve": False, "reason": reason}))


@app.command()
def stop():
    """EMERGENCY STOP: halts all actions and parks runs until `forge resume`."""
    _print(_c().post("/stop"))


@app.command()
def resume():
    _print(_c().post("/resume"))


@app.command("install-startup")
def install_startup():
    """Start forged at logon in the interactive session (needed for desktop automation; Session 0 services cannot see the GUI)."""
    pyw = Path(sys.executable).with_name("pythonw.exe")
    exe = pyw if pyw.exists() else Path(sys.executable)
    cmd = f'"{exe}" -m forge.cli daemon'
    r = subprocess.run(["schtasks", "/Create", "/TN", TASK_NAME, "/SC", "ONLOGON", "/RL", "LIMITED", "/F", "/TR", cmd,
                        "/DELAY", "0000:30"], capture_output=True, text=True)
    typer.echo(r.stdout or r.stderr)


@app.command("uninstall-startup")
def uninstall_startup():
    r = subprocess.run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"], capture_output=True, text=True)
    typer.echo(r.stdout or r.stderr)


@app.command()
def bench(suite: str = typer.Argument("desktop"), runs: int = 3, modes: str = "uia,laya,llm"):
    """Run a benchmark suite (desktop: deterministic UIA vs Laya+escalation vs LLM-every-step)."""
    if suite == "desktop":
        from forge.desktop.bench import main
        main(runs=runs, modes=modes.split(","))
    elif suite == "laya":
        from forge.desktop.bench import laya_calibration
        laya_calibration()


if __name__ == "__main__":
    app()
