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

app = typer.Typer(invoke_without_command=True, add_completion=False,
                  help="Forge: describe an automation once; Forge builds, verifies, deploys and keeps it running.\n\n"
                       "Run `forge` with no arguments to set up (first time), start the background service and open the TUI.")
wf = typer.Typer(help="workflows")
app.add_typer(wf, name="workflow")
cfg_app = typer.Typer(help="view and change settings (forge.toml)")
app.add_typer(cfg_app, name="config")
sess_app = typer.Typer(help="chat sessions")
app.add_typer(sess_app, name="sessions")


@app.callback()
def main(ctx: typer.Context,
         cont: bool = typer.Option(False, "--continue", "-c", help="resume the most recent chat session"),
         resume: str = typer.Option(None, "--resume", "-r", help="resume a session by id")):
    """Bare `forge`: setup if needed -> ensure the daemon is running -> open the TUI."""
    if ctx.invoked_subcommand is not None:
        return
    from forge.config import init_home, reload_config
    init_home()
    if not reload_config().setup.completed:
        typer.secho("First run: let's set Forge up (re-run any time with `forge setup`).", fg="cyan")
        from forge.setup.wizard import run as setup_run
        setup_run()
    from forge import launcher
    if not launcher.daemon_status():
        _ensure_ollama()
        typer.echo("Starting Forge in the background…", nl=False)
        try:
            launcher.ensure_daemon(on_wait=lambda: typer.echo(".", nl=False))
        except RuntimeError as e:
            typer.secho(f"\n{e}", fg="red")
            raise typer.Exit(1)
        typer.echo(" ready")
    from forge.tui.app import run
    run(session=resume, continue_last=cont)


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
def tui(cont: bool = typer.Option(False, "--continue", "-c"), resume: str = typer.Option(None, "--resume", "-r")):
    """Open the terminal UI only (the daemon must be running; bare `forge` starts it for you)."""
    from forge.tui.app import run
    run(session=resume, continue_last=cont)


@app.command()
def setup(yes: bool = typer.Option(False, "--yes", "-y", help="accept safe defaults without prompting"),
          profile: str = typer.Option(None, help="gpu-8gb | gpu-12gb | gpu-12gb-qwen35 | cpu-only"),
          install_ollama: bool = typer.Option(False, help="with --yes: allow installing Ollama via winget"),
          startup: bool = typer.Option(None, help="with --yes: install (or not) the start-at-login task"),
          no_pull: bool = typer.Option(False, help="skip model downloads")):
    """First-run wizard: hardware check, model profile, Ollama + models, browser, Laya, start-at-login. Idempotent."""
    from forge.setup.wizard import run as setup_run
    raise typer.Exit(setup_run(yes=yes, profile=profile, install_ollama=install_ollama, startup=startup, pull=not no_pull))


@app.command()
def doctor(as_json: bool = typer.Option(False, "--json")):
    """Check the environment and Forge's health; prints a fix for every problem."""
    from forge.setup.doctor import as_dicts, run_checks
    checks = run_checks()
    if as_json:
        _print(as_dicts(checks))
        raise typer.Exit(0)
    icon = {"ok": ("✓", "green"), "warn": ("!", "yellow"), "fail": ("✗", "red")}
    group = None
    for c in checks:
        if c.group != group:
            group = c.group
            typer.secho(f"\n{group}", bold=True)
        i, col = icon[c.status]
        typer.secho(f"  {i} {c.name:<22}", fg=col, nl=False)
        typer.echo(f" {c.detail}")
        if c.fix:
            typer.secho(f"      fix: {c.fix}", fg="bright_black")
    raise typer.Exit(1 if any(c.status == "fail" for c in checks) else 0)


@app.command()
def restart():
    """Restart the background daemon (applies restart-only settings)."""
    from forge import launcher
    launcher.stop_daemon()
    _ensure_ollama()
    launcher.ensure_daemon()
    typer.echo("forged restarted")


@app.command()
def shutdown():
    """Stop the background daemon. Scheduled workflows will not run until Forge starts again."""
    from forge import launcher
    typer.echo("stopped" if launcher.stop_daemon() else "could not stop forged")


@cfg_app.command("list")
def config_list(section: str = typer.Argument(None)):
    from forge import settings
    from forge.config import reload_config
    for s in settings.describe(reload_config()):
        if section and not s.key.startswith(section):
            continue
        val = json.dumps(s.value, default=str) if not isinstance(s.value, str) else s.value
        typer.secho(f"{s.key:<32}", bold=True, nl=False)
        typer.echo(f" {val[:70]}" + (" [restart]" if s.apply == "restart" else ""))


@cfg_app.command("get")
def config_get(key: str):
    from forge import settings
    from forge.config import reload_config
    s = settings.find(reload_config(), key)
    _print({"key": s.key, "value": s.value, "default": s.default, "type": s.type, "apply": s.apply, "description": s.description})


@cfg_app.command("set")
def config_set(key: str, value: str, confirm: bool = typer.Option(False, "--confirm", help="required for security-sensitive keys")):
    """Set a value. Goes through the daemon when it's running (applied live + audited), else edits forge.toml."""
    from forge import launcher, settings
    from forge.client import ClientError, SyncClient
    if launcher.daemon_status():
        try:
            _print(SyncClient().post("/settings", {"key": key, "value": value, "confirm": confirm}))
        except ClientError as e:
            typer.secho(str(e), fg="red")
            raise typer.Exit(1)
        return
    from forge.db import DB
    from forge.errors import InvalidInput
    try:
        res = settings.set_value(key, value, confirm_danger=confirm)
    except InvalidInput as e:
        typer.secho(str(e), fg="red")
        raise typer.Exit(1)
    DB().audit("user", "config.changed", {"key": key, "old": res["old"], "new": res["new"]})
    _print(res)


@cfg_app.command("path")
def config_path_cmd():
    from forge.config import config_path
    typer.echo(str(config_path()))


@cfg_app.command("edit")
def config_edit():
    """Open forge.toml in $EDITOR (notepad on Windows); validates on close."""
    import tomllib
    from forge.config import ForgeConfig, config_path
    p = config_path()
    editor = os.environ.get("EDITOR") or ("notepad" if sys.platform == "win32" else "nano")
    subprocess.run([editor, str(p)])
    try:
        ForgeConfig.model_validate(tomllib.loads(p.read_text(encoding="utf-8")))
        typer.secho("forge.toml is valid. Run `forge restart` (or /reload in the TUI) to apply.", fg="green")
    except Exception as e:
        typer.secho(f"forge.toml is INVALID: {e}\nPrevious versions are in ~/.forge/backups/.", fg="red")
        raise typer.Exit(1)


@sess_app.command("list")
def sessions_list():
    from forge.db import DB
    for s in DB().all("SELECT id,title,status,updated_at FROM sessions ORDER BY updated_at DESC LIMIT 30"):
        typer.echo(f"{s['id']}  {time.strftime('%m-%d %H:%M', time.localtime(s['updated_at']))}  {s['status']:<11} {s['title']}")


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
    from forge import launcher
    typer.echo(launcher.install_startup())


@app.command("uninstall-startup")
def uninstall_startup():
    from forge import launcher
    typer.echo(launcher.uninstall_startup())


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
