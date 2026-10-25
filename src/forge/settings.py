"""User-editable settings over forge.toml.

* Every change is parsed, then the WHOLE resulting config is validated by ForgeConfig before anything is written,
  so a bad value can never leave Forge unbootable.
* tomlkit preserves the file's comments and layout; the previous file is copied to backups/ first.
* Each key is `live` (applied to the running daemon immediately) or `restart` (needs `forge restart`).
* Settings are a user surface only: there is deliberately no agent tool that can change them, and the daemon audits
  every change. Security-sensitive keys are marked `danger` and need explicit confirmation.
"""
from __future__ import annotations

import datetime as dt
import json
import shutil
import tomllib
from dataclasses import asdict, dataclass, field
from typing import Any, get_args, get_origin

import tomlkit
from pydantic import BaseModel, ValidationError

from forge.config import ForgeConfig, config_path, forge_home, init_home
from forge.errors import InvalidInput

SECTIONS = {
    "daemon": "Daemon", "laya": "Laya (System 1)", "routing": "Models & Routing", "models": "Models & Routing",
    "nodes": "Nodes", "tools": "Tools", "tui": "Terminal UI", "mcp_servers": "MCP servers", "setup": "Setup",
    "workspace": "General", "notify_toast": "General",
}
RESTART_KEYS = {"daemon.host", "daemon.port", "nodes", "mcp_servers", "laya.device"}
HIDDEN = {"setup.completed"}
LOOPBACK = {"127.0.0.1", "localhost", "::1"}
DESCRIPTIONS = {
    "daemon.host": "Bind address. Keep 127.0.0.1; reach Forge remotely over SSH instead of exposing the API.",
    "daemon.port": "Local API port.",
    "daemon.max_concurrent_runs": "Workflow runs executed at the same time.",
    "daemon.lease_seconds": "Run lease; a crashed worker's runs are reclaimed after this long.",
    "daemon.scheduler_tick_seconds": "How often triggers and the run queue are checked.",
    "daemon.gui_idle_seconds": "Unattended desktop steps wait until you've been idle this long.",
    "laya.enabled": "Use Laya for fast System 1 decisions (LLM fallback when unsure).",
    "laya.checkpoint": "english (512 ctx), multilingual (100+ languages), typed-decisions.",
    "laya.accept_threshold": "Raw Laya confidence needed to act without escalating to the LLM. Refit with `forge bench laya`.",
    "laya.verify_threshold": "Confidence needed for Laya to confirm a step succeeded.",
    "laya.device": "cuda / cpu / leave empty for auto.",
    "routing.pins": "Force a model for a capability, e.g. {\"reasoning\": \"qwen3:8b\"}.",
    "tools.disabled": "Tool globs switched off for the agent and all workflows, e.g. [\"desktop.*\"].",
    "tui.theme": "Textual theme name.",
    "tui.show_timeline": "Show the tool timeline beside the chat.",
    "tui.bell_on_approval": "Ring the terminal bell when an approval is needed.",
    "workspace": "Folder the agent may write to in chat sessions (empty = ~/.forge/workspace).",
    "notify_toast": "Show Windows toast notifications.",
    "models": "Models and the capabilities they serve (JSON list).",
    "nodes": "Inference nodes (JSON list). Restart required.",
    "mcp_servers": "MCP servers to bridge (JSON list). Restart required.",
    "setup.profile": "Hardware profile chosen in `forge setup`.",
}
CHOICES = {"laya.checkpoint": ["english", "multilingual", "typed-decisions"],
           "tui.theme": ["textual-dark", "textual-light", "nord", "gruvbox", "tokyo-night", "monokai", "dracula",
                         "catppuccin-mocha", "solarized-light"]}


@dataclass
class Setting:
    key: str
    type: str                  # bool | int | float | str | list | dict | json
    section: str
    value: Any
    default: Any
    description: str = ""
    choices: list[str] = field(default_factory=list)
    apply: str = "live"
    danger: bool = False


def _type_name(ann) -> str:
    origin = get_origin(ann)
    if origin in (list,):
        inner = get_args(ann)[0] if get_args(ann) else str
        return "json" if isinstance(inner, type) and issubclass(inner, BaseModel) else "list"
    if origin is dict:
        return "dict"
    args = [a for a in get_args(ann) if a is not type(None)]
    if args:
        return _type_name(args[0])
    return {bool: "bool", int: "int", float: "float", str: "str"}.get(ann, "json")


def describe(cfg: ForgeConfig) -> list[Setting]:
    out: list[Setting] = []
    defaults = ForgeConfig()
    for name, f in ForgeConfig.model_fields.items():
        ann = f.annotation
        sub = getattr(cfg, name)
        if isinstance(ann, type) and issubclass(ann, BaseModel):
            for sname, sf in ann.model_fields.items():
                key = f"{name}.{sname}"
                if key in HIDDEN:
                    continue
                out.append(Setting(key, _type_name(sf.annotation), SECTIONS.get(name, name), getattr(sub, sname),
                                   getattr(getattr(defaults, name), sname), DESCRIPTIONS.get(key, sf.description or ""),
                                   CHOICES.get(key, []), "restart" if key in RESTART_KEYS else "live", key == "daemon.host"))
        else:
            val = [x.model_dump() for x in sub] if isinstance(sub, list) and sub and isinstance(sub[0], BaseModel) else sub
            out.append(Setting(name, _type_name(ann), SECTIONS.get(name, "General"), val, getattr(defaults, name) if not isinstance(
                getattr(defaults, name), list) else [], DESCRIPTIONS.get(name, ""), CHOICES.get(name, []),
                "restart" if name in RESTART_KEYS else "live"))
    return out


def find(cfg: ForgeConfig, key: str) -> Setting:
    for s in describe(cfg):
        if s.key == key:
            return s
    raise InvalidInput(f"unknown setting '{key}'; see `forge config list`")


def parse_value(setting: Setting, raw: Any) -> Any:
    if not isinstance(raw, str):
        return raw
    t = setting.type
    try:
        if t == "bool":
            low = raw.strip().lower()
            if low not in ("true", "false", "on", "off", "yes", "no", "1", "0"):
                raise ValueError("expected true/false")
            return low in ("true", "on", "yes", "1")
        if t == "int":
            return int(raw)
        if t == "float":
            return float(raw)
        if t == "list":
            raw = raw.strip()
            return json.loads(raw) if raw.startswith("[") else [x.strip() for x in raw.split(",") if x.strip()]
        if t in ("dict", "json"):
            return json.loads(raw)
        return raw
    except (ValueError, json.JSONDecodeError) as e:
        raise InvalidInput(f"{setting.key}: cannot parse {raw!r} as {t}: {e}") from None


def _set_path(doc, key: str, value: Any) -> None:
    parts = key.split(".")
    node = doc
    for p in parts[:-1]:
        if p not in node:
            node[p] = tomlkit.table()
        node = node[p]
    last = parts[-1]
    if isinstance(value, list) and value and isinstance(value[0], dict):
        aot = tomlkit.aot()
        for d in value:
            t = tomlkit.table()
            for k, v in d.items():
                if v is not None:
                    t[k] = v
            aot.append(t)
        node[last] = aot
    elif isinstance(value, dict):
        t = tomlkit.inline_table()
        t.update(value)
        node[last] = t
    elif value is None:
        if last in node:
            del node[last]
    else:
        node[last] = value


def set_value(key: str, raw: Any, *, confirm_danger: bool = False) -> dict:
    """Validate and persist one setting. Returns {key, old, new, apply}. Raises InvalidInput without writing."""
    init_home()
    path = config_path()
    text = path.read_text(encoding="utf-8")
    cur = ForgeConfig.model_validate(tomllib.loads(text))
    setting = find(cur, key)
    value = parse_value(setting, raw)
    if setting.danger and str(value) not in LOOPBACK and not confirm_danger:
        raise InvalidInput(f"{key}={value!r} exposes the Forge API beyond this machine; pass confirm to proceed")
    doc = tomlkit.parse(text)
    _set_path(doc, key, value)
    new_text = tomlkit.dumps(doc)
    try:
        ForgeConfig.model_validate(tomllib.loads(new_text))
    except ValidationError as e:
        errs = "; ".join(f"{'.'.join(map(str, er['loc']))}: {er['msg']}" for er in e.errors()[:5])
        raise InvalidInput(f"invalid value for {key}: {errs}") from None
    bdir = forge_home() / "backups"
    bdir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, bdir / f"forge.toml.{dt.datetime.now():%Y%m%d-%H%M%S-%f}")
    path.write_text(new_text, encoding="utf-8")
    return {"key": key, "old": setting.value, "new": value, "apply": setting.apply}


def apply_live(services, new: ForgeConfig) -> None:
    """Push a re-read config into the running daemon. Restart-only keys keep their old effect until restart."""
    old = services.cfg
    new.daemon.host, new.daemon.port = old.daemon.host, old.daemon.port   # bound sockets can't move live
    new.nodes, new.mcp_servers = old.nodes, old.mcp_servers
    services.cfg = new
    services.router.reconfigure(new)
    services.registry.disabled = list(new.tools.disabled)
    if new.laya.enabled and services.laya is None:
        from forge.models.laya import LayaEngine
        services.laya = LayaEngine(new.laya)
    elif services.laya is not None:
        services.laya.cfg = new.laya
    try:
        from forge.desktop import tools as dtools
        dtools.LEASE.idle_required_s = new.daemon.gui_idle_seconds
    except Exception:
        pass


def as_dicts(cfg: ForgeConfig) -> list[dict]:
    return [asdict(s) for s in describe(cfg)]
