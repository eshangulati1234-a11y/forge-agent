"""Start, find and stop the background daemon, and manage start-at-login. Used by bare `forge`, the CLI and setup."""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import httpx

from forge.config import api_token, forge_home, load_config

TASK_NAME = "Forge Daemon"
DETACHED = 0x00000008 | 0x00000200 | 0x08000000   # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW


def base_url() -> str:
    cfg = load_config()
    return f"http://{cfg.daemon.host}:{cfg.daemon.port}"


def daemon_status(timeout: float = 2.0) -> dict | None:
    try:
        r = httpx.get(f"{base_url()}/status", headers={"Authorization": f"Bearer {api_token()}"}, timeout=timeout)
        return r.json() if r.status_code == 200 else None
    except httpx.HTTPError:
        return None


def pid_file() -> Path:
    return forge_home() / "forged.pid"


def _python(windowless: bool = True) -> str:
    exe = Path(sys.executable)
    pyw = exe.with_name("pythonw.exe")
    return str(pyw if windowless and pyw.exists() else exe)


def spawn_daemon() -> int:
    log_dir = forge_home() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    out = open(log_dir / "forged.stdout.log", "ab")
    kwargs = {"creationflags": DETACHED} if sys.platform == "win32" else {"start_new_session": True}
    p = subprocess.Popen([_python(), "-m", "forge.cli", "daemon"], stdout=out, stderr=out, stdin=subprocess.DEVNULL,
                         cwd=str(forge_home()), env={**os.environ}, **kwargs)
    pid_file().write_text(str(p.pid))
    return p.pid


def log_tail(lines: int = 25) -> str:
    p = forge_home() / "logs" / "forged.log"
    if not p.exists():
        p = forge_home() / "logs" / "forged.stdout.log"
    return "\n".join(p.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:]) if p.exists() else "(no log yet)"


def ensure_daemon(timeout: float = 30.0, on_wait=None) -> dict:
    st = daemon_status()
    if st:
        return st
    spawn_daemon()
    end = time.time() + timeout
    while time.time() < end:
        time.sleep(0.5)
        if on_wait:
            on_wait()
        st = daemon_status(1.0)
        if st:
            return st
    raise RuntimeError(f"forged did not start within {timeout:.0f}s. Last log lines:\n{log_tail()}")


def stop_daemon(timeout: float = 15.0) -> bool:
    try:
        httpx.post(f"{base_url()}/shutdown", headers={"Authorization": f"Bearer {api_token()}"}, timeout=3)
    except httpx.HTTPError:
        pass
    end = time.time() + timeout
    while time.time() < end:
        if not daemon_status(0.5):
            _reap(timeout=5.0)
            return True
        time.sleep(0.4)
    pid = _port_pid(load_config().daemon.port)
    if pid:
        import psutil
        psutil.Process(pid).kill()
    _reap(timeout=0)
    return not daemon_status(0.5)


def _reap(timeout: float) -> None:
    """The API going quiet doesn't mean the process exited: make sure the process tree we spawned is gone."""
    import psutil
    try:
        root = psutil.Process(int(pid_file().read_text().strip()))
        procs = [root, *root.children(recursive=True)]
    except (OSError, ValueError, psutil.Error):
        return
    procs = [p for p in procs if "forge.cli" in " ".join(_cmdline(p))]   # never a reused PID's process
    _, alive = psutil.wait_procs(procs, timeout=timeout)
    for p in alive:
        try:
            p.kill()
        except psutil.Error:
            pass


def _cmdline(p) -> list[str]:
    import psutil
    try:
        return p.cmdline()
    except psutil.Error:
        return []


def _port_pid(port: int) -> int | None:
    import psutil
    for c in psutil.net_connections(kind="tcp"):
        if c.laddr and c.laddr.port == port and c.status == psutil.CONN_LISTEN:
            return c.pid
    return None


def startup_installed() -> bool:
    if sys.platform != "win32":
        return False
    r = subprocess.run(["schtasks", "/Query", "/TN", TASK_NAME], capture_output=True, text=True, creationflags=0x08000000)
    return r.returncode == 0


def install_startup() -> str:
    """Logon task in the interactive session (a Session 0 service cannot drive the desktop)."""
    cmd = f'"{_python()}" -m forge.cli daemon'
    r = subprocess.run(["schtasks", "/Create", "/TN", TASK_NAME, "/SC", "ONLOGON", "/RL", "LIMITED", "/F", "/TR", cmd,
                        "/DELAY", "0000:30"], capture_output=True, text=True)
    return (r.stdout or r.stderr).strip()


def uninstall_startup() -> str:
    r = subprocess.run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"], capture_output=True, text=True)
    return (r.stdout or r.stderr).strip()
