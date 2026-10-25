"""Hardware / environment detection for setup and doctor. Everything here is read-only and fails soft."""
from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

import httpx

NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


@dataclass
class Gpu:
    name: str
    vram_mb: int
    used_mb: int = 0
    driver: str = ""


@dataclass
class Hardware:
    os: str
    python: str
    ram_gb: float
    gpus: list[Gpu] = field(default_factory=list)
    cuda_torch: bool | None = None
    ollama_path: str | None = None
    ollama_version: str | None = None
    session_id: int | None = None
    display_scale: float | None = None

    @property
    def max_vram_mb(self) -> int:
        return max((g.vram_mb for g in self.gpus), default=0)

    def dict(self) -> dict:
        return asdict(self)


def gpus() -> list[Gpu]:
    exe = shutil.which("nvidia-smi")
    if not exe:
        return []
    try:
        out = subprocess.run([exe, "--query-gpu=name,memory.total,memory.used,driver_version", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=8, creationflags=NO_WINDOW).stdout
    except Exception:
        return []
    res = []
    for line in out.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 4:
            res.append(Gpu(parts[0], int(parts[1]), int(parts[2]), parts[3]))
    return res


def ollama_path() -> str | None:
    p = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama.exe"
    if p.exists():
        return str(p)
    return shutil.which("ollama")


def ollama_version(url: str = "http://127.0.0.1:11434") -> str | None:
    try:
        return httpx.get(f"{url}/api/version", timeout=2).json().get("version")
    except Exception:
        return None


def ollama_models(url: str = "http://127.0.0.1:11434") -> set[str] | None:
    try:
        return {m["name"] for m in httpx.get(f"{url}/api/tags", timeout=4).json().get("models", [])}
    except Exception:
        return None


def session_id() -> int | None:
    if sys.platform != "win32":
        return None
    import ctypes
    import ctypes.wintypes as wt
    sid = wt.DWORD()
    ok = ctypes.windll.kernel32.ProcessIdToSessionId(os.getpid(), ctypes.byref(sid))
    return int(sid.value) if ok else None


def cuda_torch() -> bool | None:
    try:
        import torch
        return bool(torch.cuda.is_available())
    except Exception:
        return None


def detect(check_torch: bool = True) -> Hardware:
    import psutil
    scale = None
    try:
        from forge.desktop.session import display_info
        scale = display_info().get("scale")
    except Exception:
        pass
    return Hardware(os=f"{platform.system()} {platform.release()} ({platform.version()})", python=platform.python_version(),
                    ram_gb=round(psutil.virtual_memory().total / 2**30, 1), gpus=gpus(),
                    cuda_torch=cuda_torch() if check_torch else None, ollama_path=ollama_path(), ollama_version=ollama_version(),
                    session_id=session_id(), display_scale=scale)
