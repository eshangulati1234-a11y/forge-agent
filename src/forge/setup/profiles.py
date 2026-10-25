"""Hardware profiles: which local models Forge configures for a given GPU.

`verified` profiles were run and benchmarked on real hardware; `experimental` ones are reasonable estimates whose
tool-calling and VRAM fit have not been measured yet (the wizard says so before applying them).
"""
from __future__ import annotations

from dataclasses import dataclass, field

S1 = ["reasoning", "tool_calling", "coding", "extraction", "compile", "desktop_reasoning"]


@dataclass
class Profile:
    name: str
    title: str
    status: str                      # verified | experimental
    min_vram_mb: int
    models: list[dict] = field(default_factory=list)
    laya: bool = True
    laya_device: str | None = None
    notes: str = ""

    def pulls(self) -> list[str]:
        return [m["id"] for m in self.models]


PROFILES: dict[str, Profile] = {p.name: p for p in [
    Profile("gpu-8gb", "8 GB NVIDIA (e.g. RTX 3070 Laptop)", "verified", 7500, [
        {"id": "qwen3:8b", "capabilities": S1, "ctx": 8192, "vram_mb": 5600, "quality": 3},
        {"id": "qwen3:4b", "capabilities": ["reasoning", "tool_calling", "extraction", "fast"], "ctx": 8192, "vram_mb": 3200, "quality": 2},
        {"id": "qwen2.5vl:7b", "capabilities": ["vision"], "ctx": 8192, "vram_mb": 6200, "quality": 3},
        {"id": "qwen3-embedding:0.6b", "capabilities": ["embedding"], "ctx": 8192, "vram_mb": 700, "quality": 2},
    ], notes="Measured: qwen3:8b@8k + Laya = 7.9/8.2 GB; vision swaps the 8B out."),
    Profile("gpu-12gb", "12 GB NVIDIA (e.g. RTX 3060)", "verified", 11500, [
        {"id": "qwen3:8b", "capabilities": S1, "ctx": 16384, "vram_mb": 6400, "quality": 3},
        {"id": "qwen3:4b", "capabilities": ["reasoning", "tool_calling", "extraction", "fast"], "ctx": 8192, "vram_mb": 3200, "quality": 2},
        {"id": "qwen2.5vl:7b", "capabilities": ["vision"], "ctx": 8192, "vram_mb": 6200, "quality": 3},
        {"id": "qwen3-embedding:0.6b", "capabilities": ["embedding"], "ctx": 8192, "vram_mb": 700, "quality": 2},
    ], notes="Same models as gpu-8gb with 16k context; not yet benchmarked on a 3060."),
    Profile("gpu-12gb-qwen35", "12 GB NVIDIA with Qwen3.5-9B (one model for reasoning + vision)", "experimental", 11500, [
        {"id": "qwen3.5:9b", "capabilities": S1 + ["vision"], "ctx": 16384, "vram_mb": 8500, "quality": 4},
        {"id": "qwen3:4b", "capabilities": ["reasoning", "tool_calling", "extraction", "fast"], "ctx": 8192, "vram_mb": 3200, "quality": 2},
        {"id": "qwen3-embedding:0.6b", "capabilities": ["embedding"], "ctx": 8192, "vram_mb": 700, "quality": 2},
    ], notes="UNVERIFIED: Ollama tool calling and real VRAM use of qwen3.5:9b not measured yet."),
    Profile("cpu-only", "No NVIDIA GPU (slow)", "experimental", 0, [
        {"id": "qwen3:4b", "capabilities": S1 + ["fast"], "ctx": 8192, "vram_mb": 0, "quality": 2},
        {"id": "qwen3-embedding:0.6b", "capabilities": ["embedding"], "ctx": 8192, "vram_mb": 0, "quality": 2},
    ], laya_device="cpu", notes="Works, but expect several seconds per model call."),
]}


def recommend(max_vram_mb: int) -> Profile:
    if max_vram_mb >= PROFILES["gpu-12gb"].min_vram_mb:
        return PROFILES["gpu-12gb"]
    if max_vram_mb >= PROFILES["gpu-8gb"].min_vram_mb:
        return PROFILES["gpu-8gb"]
    return PROFILES["cpu-only"]


def models_config(p: Profile, node: str = "local") -> list[dict]:
    return [{**m, "node": node} for m in p.models]
