"""`forge setup`: idempotent first-run wizard. Safe to re-run; every step skips work that is already done.

It never installs system software or a startup task without an explicit yes (interactive) or an explicit flag
(--install-ollama / --startup with --yes).
"""
from __future__ import annotations

import json
import subprocess
import sys

import httpx
from rich.console import Console
from rich.panel import Panel
from rich.progress import BarColumn, DownloadColumn, Progress, TextColumn, TransferSpeedColumn
from rich.prompt import Confirm, Prompt
from rich.table import Table

from forge import __version__
from forge.config import init_home, reload_config
from forge.setup import hardware
from forge.setup.profiles import PROFILES, models_config, recommend

console = Console()


def _ask(question: str, default: bool, yes: bool, flag: bool | None = None) -> bool:
    if yes:
        return bool(flag) if flag is not None else default
    return Confirm.ask(question, default=default)


def pull_model(url: str, model: str) -> bool:
    with Progress(TextColumn(f"[cyan]{model}[/cyan] {{task.description}}"), BarColumn(), DownloadColumn(), TransferSpeedColumn(),
                  console=console, transient=False) as prog:
        task = prog.add_task("pulling", total=None)
        try:
            with httpx.stream("POST", f"{url}/api/pull", json={"model": model, "stream": True}, timeout=None) as r:
                for line in r.iter_lines():
                    if not line:
                        continue
                    d = json.loads(line)
                    if "error" in d:
                        prog.update(task, description=f"[red]{d['error']}[/red]")
                        return False
                    if d.get("total"):
                        prog.update(task, total=d["total"], completed=d.get("completed", 0), description=d.get("status", "")[:30])
                    else:
                        prog.update(task, description=d.get("status", "")[:30])
                    if d.get("status") == "success":
                        return True
        except httpx.HTTPError as e:
            prog.update(task, description=f"[red]{e}[/red]")
            return False
    return True


def run(yes: bool = False, profile: str | None = None, install_ollama: bool = False, startup: bool | None = None,
        pull: bool = True, warm_laya: bool = True) -> int:
    from forge import settings
    init_home()
    cfg = reload_config()
    console.print(Panel.fit(f"[b]Forge setup[/b] v{__version__}\nDescribe an automation once; Forge builds, verifies, deploys and keeps it running.",
                            border_style="cyan"))

    # 1. hardware
    hw = hardware.detect()
    t = Table(show_header=False, box=None)
    t.add_row("OS", hw.os)
    t.add_row("Python", hw.python)
    t.add_row("RAM", f"{hw.ram_gb} GB")
    for g in hw.gpus:
        t.add_row("GPU", f"{g.name} — {g.vram_mb} MB (driver {g.driver})")
    if not hw.gpus:
        t.add_row("GPU", "[yellow]no NVIDIA GPU found[/yellow]")
    t.add_row("CUDA torch", "yes" if hw.cuda_torch else "[yellow]no[/yellow]")
    t.add_row("Ollama", f"v{hw.ollama_version} (running)" if hw.ollama_version else ("installed, not running" if hw.ollama_path else "[yellow]not installed[/yellow]"))
    if hw.session_id == 0:
        t.add_row("Session", "[red]Session 0 — desktop automation will not work here[/red]")
    console.print(t)

    # 2. profile
    rec = recommend(hw.max_vram_mb)
    if profile is None and not yes:
        console.print("\n[b]Model profiles[/b]")
        for p in PROFILES.values():
            mark = " [green](recommended)[/green]" if p.name == rec.name else ""
            tag = "[green]verified[/green]" if p.status == "verified" else "[yellow]experimental[/yellow]"
            console.print(f"  [b]{p.name}[/b] — {p.title} · {tag}{mark}\n     {', '.join(p.pulls())}\n     [dim]{p.notes}[/dim]")
        profile = Prompt.ask("Profile", choices=list(PROFILES), default=cfg.setup.profile or rec.name)
    prof = PROFILES[profile or cfg.setup.profile or rec.name]
    if prof.status == "experimental":
        console.print(f"[yellow]⚠ {prof.name} is experimental: {prof.notes}[/yellow]")

    # 3. ollama
    node = next((n for n in cfg.nodes if n.kind == "ollama"), None)
    url = node.url if node else "http://127.0.0.1:11434"
    if not hw.ollama_path:
        if sys.platform == "win32" and _ask("Ollama is not installed. Install it now with winget?", True, yes, install_ollama):
            subprocess.run(["winget", "install", "--id", "Ollama.Ollama", "-e", "--accept-source-agreements", "--accept-package-agreements"])
        else:
            console.print("[yellow]Skipping Ollama install. Install it from https://ollama.com and re-run `forge setup`.[/yellow]")
    if not hardware.ollama_version(url):
        from forge.cli import _ensure_ollama
        _ensure_ollama()

    # 4. models
    node_name = node.name if node else "local"
    if pull and hardware.ollama_version(url):
        have = hardware.ollama_models(url) or set()
        missing = [m for m in prof.pulls() if m not in have and f"{m}:latest" not in have]
        if missing:
            console.print(f"\nPulling {len(missing)} model(s): {', '.join(missing)}")
            for m in missing:
                if not pull_model(url, m):
                    console.print(f"[red]failed to pull {m}[/red]")
        else:
            console.print("[green]✓[/green] all profile models already pulled")

    # 5. write config (comments preserved)
    settings.set_value("models", models_config(prof, node_name))
    settings.set_value("laya.enabled", prof.laya)
    if prof.laya_device:
        settings.set_value("laya.device", prof.laya_device)
    settings.set_value("setup.profile", prof.name)
    console.print("[green]✓[/green] forge.toml updated")

    # 6. browser
    from forge.setup.doctor import check_playwright
    if check_playwright(cfg).status != "ok":
        console.print("Installing Playwright Chromium…")
        subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"])

    # 7. laya checkpoint
    if prof.laya and warm_laya:
        from forge.setup.doctor import check_laya
        if check_laya(reload_config()).status != "ok":
            with console.status("Downloading the Laya checkpoint (~1-2 GB, one time)…"):
                from forge.models.laya import LayaEngine
                eng = LayaEngine(reload_config().laya)
                eng.yes_no_sync("setup check", "Is this a test?")
                eng.unload()
        console.print("[green]✓[/green] Laya ready")

    # 8. signing key
    from forge.policy.approvals import _signing_key
    _signing_key()

    # 9. startup
    from forge import launcher
    if sys.platform == "win32" and not launcher.startup_installed():
        if _ask("Start Forge automatically when you log in? (needed for unattended schedules)", False, yes, startup):
            console.print(launcher.install_startup())

    # 10. doctor + done
    _mark_complete()
    from forge.setup.doctor import run_checks
    checks = run_checks(reload_config(), skip={"daemon"})
    bad = [c for c in checks if c.status != "ok"]
    for c in bad:
        col = "yellow" if c.status == "warn" else "red"
        console.print(f"[{col}]• {c.name}: {c.detail}[/{col}]" + (f"\n    fix: {c.fix}" if c.fix else ""))
    console.print(Panel.fit("[b green]Setup complete.[/b green] Run [b]forge[/b] to start." if not any(c.status == "fail" for c in bad)
                            else "[b yellow]Setup finished with problems above.[/b yellow] `forge doctor` re-checks.", border_style="green"))
    return 0 if not any(c.status == "fail" for c in bad) else 1


def _mark_complete() -> None:
    """setup.completed is hidden from the settings UI, so it is written directly (still comment-preserving)."""
    import tomlkit
    from forge.config import config_path
    p = config_path()
    doc = tomlkit.parse(p.read_text(encoding="utf-8"))
    if "setup" not in doc:
        doc["setup"] = tomlkit.table()
    doc["setup"]["completed"] = __version__
    p.write_text(tomlkit.dumps(doc), encoding="utf-8")
