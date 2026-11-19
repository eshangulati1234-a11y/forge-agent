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

from forge._plat import win_flags



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


def is_apple_silicon() -> bool:
    return sys.platform == "darwin" and platform.machine() == "arm64"


def apple_gpu() -> Gpu | None:
    """Apple Silicon shares unified memory with the GPU; Metal can use roughly 2/3 of RAM on smaller Macs, 3/4 on larger."""
    if not is_apple_silicon():
        return None
    import psutil
    total_mb = psutil.virtual_memory().total // 2**20
    name = "Apple Silicon"
    try:
        name = subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"], capture_output=True, text=True, timeout=3).stdout.strip() or name
    except Exception:
        pass
    usable = int(total_mb * (0.75 if total_mb >= 36 * 1024 else 0.66))
    return Gpu(f"{name} (Metal, unified memory)", usable, 0, "")


def gpus() -> list[Gpu]:
    apple = apple_gpu()
    if apple:
        return [apple]
    exe = shutil.which("nvidia-smi")
    if not exe:
        return []
    try:
        out = subprocess.run([exe, "--query-gpu=name,memory.total,memory.used,driver_version", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=8, **win_flags()).stdout
    except Exception:
        return []
    res = []
    for line in out.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 4:
            res.append(Gpu(parts[0], int(parts[1]), int(parts[2]), parts[3]))
    return res


def ollama_path() -> str | None:
    if sys.platform == "win32":
        p = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama.exe"
        if p.exists():
            return str(p)
    found = shutil.which("ollama")
    if found:
        return found
    if sys.platform == "darwin":
        for app in (Path("/Applications/Ollama.app"), Path.home() / "Applications" / "Ollama.app"):
            exe = app / "Contents" / "Resources" / "ollama"
            if exe.exists():
                return str(exe)
    return None


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


def recommended_max_loaded(model_vram_mb: list[int], usable_mb: int, cap: int = 3) -> int:
    """How many models Ollama may keep resident at once. 1 on small GPUs (loading a second evicts the first anyway);
    more on large-memory machines, so an embedding or vision call no longer evicts the main model and forces a reload.
    Counts the largest-first prefix of the configured models that fits in ~75% of usable memory (the rest is KV cache)."""
    budget = usable_mb * 0.75
    total, n = 0, 0
    for v in sorted(model_vram_mb, reverse=True)[:cap]:
        total += v
        if total > budget:
            break
        n += 1
    return max(1, n)


def session_id() -> int | None:
    if sys.platform != "win32":
        return None
    import ctypes
    import ctypes.wintypes as wt
    sid = wt.DWORD()
    ok = ctypes.windll.kernel32.ProcessIdToSessionId(os.getpid(), ctypes.byref(sid))
    return int(sid.value) if ok else None


def cuda_torch() -> bool | None:
    """True when torch has a usable accelerator: CUDA, or Metal (MPS) on Apple Silicon."""
    try:
        import torch
        if is_apple_silicon():
            return bool(torch.backends.mps.is_available())
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
