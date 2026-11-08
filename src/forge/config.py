"""Forge configuration: FORGE_HOME layout and forge.toml (nodes, models, policy)."""
from __future__ import annotations

import os
import secrets
import tomllib
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel, Field


def forge_home() -> Path:
    return Path(os.environ.get("FORGE_HOME", Path.home() / ".forge"))


class NodeConfig(BaseModel):
    name: str
    url: str                      # e.g. http://127.0.0.1:11434
    kind: str = "ollama"          # ollama | openai
    api_key: str | None = None    # bearer for remote nodes / PAIR / llama.cpp --api-key
    max_parallel: int = 1
    vram_mb: int = 8192


class ModelConfig(BaseModel):
    id: str                       # model name as the node knows it
    node: str
    capabilities: list[str]
    ctx: int = 8192
    vram_mb: int = 0
    quality: int = 1              # higher is better; used for ranking
    think: bool = False           # qwen3 thinking mode


class LayaConfig(BaseModel):
    enabled: bool = True
    checkpoint: str = "english"   # english | multilingual | typed-decisions
    device: str | None = None     # None = auto (cuda first)
    # Raw Laya probabilities are over-confident; these thresholds are refit by `forge bench`.
    accept_threshold: float = 0.6
    verify_threshold: float = 0.7


class DaemonConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8765
    max_concurrent_runs: int = 2
    lease_seconds: int = 60
    scheduler_tick_seconds: float = 2.0
    gui_idle_seconds: int = 120   # GUI tasks only run when the user has been idle this long


class McpServerConfig(BaseModel):
    name: str
    command: str
    args: list[str] = Field(default_factory=list)
    side_effect: str = "external"  # MCP tools are untrusted: default to approval-gated


class ToolsConfig(BaseModel):
    disabled: list[str] = Field(default_factory=list, description="tool name globs that are switched off everywhere")


class RoutingConfig(BaseModel):
    pins: dict[str, str] = Field(default_factory=dict, description="capability -> model id override")


class TuiConfig(BaseModel):
    theme: str = "textual-dark"
    show_timeline: bool = True
    bell_on_approval: bool = True


class BrowserConfig(BaseModel):
    executable: str = ""          # Chromium-based browser to drive (Brave, Edge, Chrome); empty = Playwright's Chromium
    headless: bool = False
    show_cursor: bool = True
    action_delay_ms: int = Field(300, ge=0, le=2000)


class SetupConfig(BaseModel):
    completed: str | None = None  # forge version that finished `forge setup`
    profile: str | None = None


class ForgeConfig(BaseModel):
    daemon: DaemonConfig = Field(default_factory=DaemonConfig)
    nodes: list[NodeConfig] = Field(default_factory=list)
    models: list[ModelConfig] = Field(default_factory=list)
    laya: LayaConfig = Field(default_factory=LayaConfig)
    mcp_servers: list[McpServerConfig] = Field(default_factory=list)
    tools: ToolsConfig = Field(default_factory=ToolsConfig)
    routing: RoutingConfig = Field(default_factory=RoutingConfig)
    tui: TuiConfig = Field(default_factory=TuiConfig)
    browser: BrowserConfig = Field(default_factory=BrowserConfig)
    setup: SetupConfig = Field(default_factory=SetupConfig)
    workspace: str = ""           # default writable dir for chat/session tools
    notify_toast: bool = True

    @property
    def home(self) -> Path:
        return forge_home()

    @property
    def workspace_path(self) -> Path:
        return Path(self.workspace).expanduser() if self.workspace else self.home / "workspace"


DEFAULT_TOML = """\
workspace = ""            # empty = ~/.forge/workspace
notify_toast = true

[daemon]
host = "127.0.0.1"        # never expose; remote access is via SSH into this machine
port = 8765
max_concurrent_runs = 2

[[nodes]]
name = "blade"
url = "http://127.0.0.1:11434"
kind = "ollama"
vram_mb = 8192

# Add the RTX 3060 desktop later, e.g.:
# [[nodes]]
# name = "desktop"
# url = "http://10.0.0.2:11434"   # bind Ollama to the VPN/LAN IP + firewall-restrict to Forge host
# kind = "ollama"
# vram_mb = 12288

[[models]]
id = "qwen3:8b"
node = "blade"
capabilities = ["reasoning", "tool_calling", "coding", "extraction", "compile", "desktop_reasoning"]
ctx = 8192
vram_mb = 5600
quality = 3

[[models]]
id = "qwen3:4b"
node = "blade"
capabilities = ["reasoning", "tool_calling", "extraction", "fast"]
ctx = 8192
vram_mb = 3200
quality = 2

[[models]]
id = "qwen2.5vl:7b"
node = "blade"
capabilities = ["vision"]
ctx = 8192
vram_mb = 6200
quality = 3

[[models]]
id = "qwen3-embedding:0.6b"
node = "blade"
capabilities = ["embedding"]
ctx = 8192
vram_mb = 700
quality = 2

[laya]
enabled = true
checkpoint = "english"
accept_threshold = 0.6
verify_threshold = 0.7
"""


def init_home() -> Path:
    home = forge_home()
    for sub in ("workspace", "workflows", "logs", "backups", "browser", "artifacts"):
        (home / sub).mkdir(parents=True, exist_ok=True)
    cfg = home / "forge.toml"
    if not cfg.exists():
        cfg.write_text(DEFAULT_TOML, encoding="utf-8")
    token = home / "token"
    if not token.exists():
        token.write_text(secrets.token_urlsafe(32), encoding="utf-8")
    return home


def config_path() -> Path:
    return forge_home() / "forge.toml"


@lru_cache(maxsize=1)
def load_config() -> ForgeConfig:
    init_home()
    data = tomllib.loads(config_path().read_text(encoding="utf-8"))
    return ForgeConfig.model_validate(data)


def reload_config() -> ForgeConfig:
    load_config.cache_clear()
    return load_config()


def api_token() -> str:
    init_home()
    return (forge_home() / "token").read_text(encoding="utf-8").strip()
