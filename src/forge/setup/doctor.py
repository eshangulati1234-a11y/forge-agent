"""`forge doctor`: environment and health checks with a concrete fix for every non-green result.
Shared by the CLI, GET /doctor and the TUI doctor overlay. Every check fails soft (never raises)."""
from __future__ import annotations

import os
import shutil
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

from forge.config import ForgeConfig, forge_home, load_config


@dataclass
class Check:
    name: str
    status: str          # ok | warn | fail
    detail: str = ""
    fix: str = ""
    group: str = "general"


def _c(name, ok, detail="", fix="", warn=False, group="general") -> Check:
    return Check(name, "ok" if ok else ("warn" if warn else "fail"), detail, "" if ok else fix, group)


def check_python(cfg) -> Check:
    v = sys.version_info
    return _c("python", v >= (3, 12), f"{v.major}.{v.minor}.{v.micro}", "install Python 3.12+ (uv python install 3.12)", group="runtime")


def check_uv(cfg) -> Check:
    p = shutil.which("uv")
    return _c("uv", bool(p), p or "not on PATH", "install uv: https://docs.astral.sh/uv/", warn=True, group="runtime")


def check_gpu(cfg) -> Check:
    from forge.setup.hardware import gpus
    g = gpus()
    if not g:
        return _c("nvidia gpu", False, "nvidia-smi not found", "install the NVIDIA driver, or use the cpu-only profile", warn=True, group="inference")
    top = max(g, key=lambda x: x.vram_mb)
    free = top.vram_mb - top.used_mb
    return _c("nvidia gpu", True, f"{top.name} {top.vram_mb} MB ({free} MB free), driver {top.driver}", group="inference")


def check_cuda_torch(cfg) -> Check:
    from forge.setup.hardware import cuda_torch
    ok = cuda_torch()
    return _c("cuda torch", bool(ok), "available" if ok else ("torch missing" if ok is None else "CPU-only torch build"),
              "uv sync (pyproject pins torch to the pytorch-cu126 index)", warn=True, group="inference")


def check_ollama(cfg) -> Check:
    from forge.setup.hardware import ollama_path, ollama_version
    node = next((n for n in cfg.nodes if n.kind == "ollama"), None)
    url = node.url if node else "http://127.0.0.1:11434"
    v = ollama_version(url)
    if v:
        return _c("ollama", True, f"v{v} at {url}", group="inference")
    if ollama_path():
        return _c("ollama", False, f"installed but not reachable at {url}", "run `forge` (it starts `ollama serve`) or `ollama serve`", group="inference")
    return _c("ollama", False, "not installed", "forge setup  (or: winget install Ollama.Ollama)", group="inference")


def check_models(cfg) -> Check:
    from forge.setup.hardware import ollama_models
    node = next((n for n in cfg.nodes if n.kind == "ollama"), None)
    have = ollama_models(node.url) if node else None
    if have is None:
        return _c("models pulled", False, "cannot list models (Ollama down?)", "start Ollama, then `forge setup`", warn=True, group="inference")
    want = [m.id for m in cfg.models if m.node == (node.name if node else "")]
    missing = [m for m in want if m not in have and f"{m}:latest" not in have]
    return _c("models pulled", not missing, f"{len(want) - len(missing)}/{len(want)} present" + (f"; missing {missing}" if missing else ""),
              "forge setup   (pulls missing models)", group="inference")


def check_vram_fit(cfg) -> Check:
    from forge.setup.hardware import gpus
    g = gpus()
    if not g:
        return _c("vram fit", True, "no GPU (cpu profile)", group="inference")
    total = max(x.vram_mb for x in g)
    main = max((m.vram_mb for m in cfg.models if "tool_calling" in m.capabilities), default=0)
    need = main + (1700 if cfg.laya.enabled and cfg.laya.device != "cpu" else 0) + 700
    return _c("vram fit", need <= total, f"main model + Laya + KV ≈ {need} MB of {total} MB",
              "pick a smaller profile (forge setup --profile gpu-8gb) or set laya.device = cpu", warn=True, group="inference")


def check_laya(cfg) -> Check:
    if not cfg.laya.enabled:
        return _c("laya", True, "disabled in settings", group="inference")
    try:
        import laya  # noqa: F401
    except Exception as e:
        return _c("laya", False, f"import failed: {e}", "uv sync", group="inference")
    hub = Path(os.environ.get("HF_HOME", Path.home() / ".cache" / "huggingface")) / "hub"
    cached = any(hub.glob("models--convaiinnovations--laya*")) if hub.exists() else False
    return _c("laya", cached, "checkpoint cached" if cached else "checkpoint not downloaded yet (first decision will take ~1-2 min)",
              "forge setup  (pre-downloads the checkpoint)", warn=True, group="inference")


def check_playwright(cfg) -> Check:
    if cfg.browser.executable:
        exe = Path(cfg.browser.executable)
        return _c("browser", exe.is_file(), str(exe), "forge config set browser.executable <path to brave.exe / msedge.exe>",
                  group="automation")
    root = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH", Path(os.environ.get("LOCALAPPDATA", Path.home())) / "ms-playwright"))
    ok = root.exists() and any(root.glob("chromium-*"))
    return _c("playwright chromium", ok, str(root), "uv run playwright install chromium", warn=True, group="automation")


def check_keyring(cfg) -> Check:
    try:
        import keyring
        from forge.policy.approvals import KEYRING_SERVICE, KEYRING_USER
        ok = bool(keyring.get_password(KEYRING_SERVICE, KEYRING_USER))
        return _c("approval signing key", True, "in Windows Credential Manager" if ok else "will be created on first approval", group="security")
    except Exception as e:
        return _c("approval signing key", False, f"keyring unavailable: {e}", "Forge falls back to ~/.forge/.approval_key (less protected)", warn=True, group="security")


def check_home(cfg) -> Check:
    home = forge_home()
    try:
        home.mkdir(parents=True, exist_ok=True)
        probe = home / ".write_probe"
        probe.write_text("x")
        probe.unlink()
        free_gb = shutil.disk_usage(home).free / 2**30
        return _c("forge home", free_gb > 5, f"{home} ({free_gb:.0f} GB free)", "free disk space (models + backups need room)", warn=True, group="state")
    except Exception as e:
        return _c("forge home", False, f"{home}: {e}", "check folder permissions", group="state")


def check_db(cfg) -> Check:
    from forge.db import DB
    from forge.migrations import LATEST
    try:
        db = DB()
        integ = db.one("PRAGMA integrity_check")
        ok_int = list(integ.values())[0] == "ok"
        chain, bad = db.verify_audit_chain()
        v = db.schema_version
        detail = f"schema v{v}/{LATEST}, integrity {'ok' if ok_int else 'FAILED'}, audit chain {'ok' if chain else f'BROKEN at #{bad}'}"
        return _c("database", ok_int and chain and v == LATEST, detail,
                  "restore the latest file from ~/.forge/backups/ (audit chain broken = possible tampering)", group="state")
    except Exception as e:
        return _c("database", False, str(e), "restore ~/.forge/backups/forge.db.* ", group="state")


def check_daemon(cfg) -> Check:
    from forge.launcher import daemon_status
    st = daemon_status()
    if st:
        return _c("daemon", True, f"running on {cfg.daemon.host}:{cfg.daemon.port}, up {int(st.get('uptime_s', 0))}s", group="state")
    return _c("daemon", False, "not running", "run `forge` (starts it in the background)", warn=True, group="state")


def check_bind(cfg) -> Check:
    loop = cfg.daemon.host in ("127.0.0.1", "localhost", "::1")
    return _c("api exposure", loop, f"bound to {cfg.daemon.host}", "set daemon.host = 127.0.0.1 and use SSH for remote access", group="security")


def check_session(cfg) -> Check:
    if sys.platform != "win32":
        return _c("desktop session", True, "n/a", group="automation")
    from forge.setup.hardware import session_id
    from forge.desktop.session import is_locked
    sid = session_id()
    if sid == 0:
        return _c("desktop session", False, "running in Session 0 (service): desktop automation impossible",
                  "run forge as a logon task: forge install-startup", group="automation")
    locked = is_locked()
    return _c("desktop session", not locked, f"session {sid}, {'locked' if locked else 'unlocked'}",
              "unlock the desktop; GUI steps defer while locked", warn=True, group="automation")


def check_startup(cfg) -> Check:
    from forge.launcher import startup_installed
    ok = startup_installed()
    return _c("start at login", ok, "logon task installed" if ok else "not installed",
              "forge install-startup  (needed for unattended schedules after reboot)", warn=True, group="state")


def check_plugins(cfg) -> Check:
    from forge.plugins import plugin_dirs
    try:
        import yaml
        ds = plugin_dirs()
        names = [yaml.safe_load((d / "plugin.yaml").read_text(encoding="utf-8"))["name"] for d in ds]
        return _c("plugins", True, ", ".join(names) or "none", group="general")
    except Exception as e:
        return _c("plugins", False, str(e), "fix or remove the broken plugin.yaml", warn=True, group="general")


CHECKS: list[Callable[[ForgeConfig], Check]] = [
    check_python, check_uv, check_gpu, check_cuda_torch, check_ollama, check_models, check_vram_fit, check_laya,
    check_playwright, check_session, check_keyring, check_bind, check_home, check_db, check_daemon, check_startup, check_plugins]


def run_checks(cfg: ForgeConfig | None = None, skip: set[str] | None = None) -> list[Check]:
    cfg = cfg or load_config()
    out = []
    for fn in CHECKS:
        if skip and fn.__name__.removeprefix("check_") in skip:
            continue
        try:
            out.append(fn(cfg))
        except Exception as e:  # a doctor must never crash
            out.append(Check(fn.__name__.removeprefix("check_"), "warn", f"check errored: {type(e).__name__}: {e}", "", "general"))
    return out


def as_dicts(checks: list[Check]) -> list[dict]:
    return [asdict(c) for c in checks]
