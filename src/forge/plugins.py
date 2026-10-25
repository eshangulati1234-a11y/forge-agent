"""Plugin loader. A plugin is a directory with plugin.yaml:

    name: careerpilot
    version: 0.1.0
    collections: collections.yaml     # record collections (fields, enums, transitions)
    tools: tools.py                   # optional python module registering @tool functions
    workflows: [job_watch.yaml]       # imported as *draft* versions; deploying still needs approval

Plugins run in-process with Forge's privileges, so only install plugins you trust. Their tools are still
subject to the policy engine (side-effect levels, grants, approvals) like every other tool.
"""
from __future__ import annotations

import importlib.util
import logging
from pathlib import Path

import yaml

from forge.config import forge_home

log = logging.getLogger("forge.plugins")


def plugin_dirs(root: Path | None = None) -> list[Path]:
    roots = [root] if root else [Path(__file__).resolve().parents[2] / "plugins", forge_home() / "plugins"]
    out = []
    for r in roots:
        if r and r.exists():
            out += [d for d in sorted(r.iterdir()) if (d / "plugin.yaml").exists()]
    return out


def load_plugins(services, root: Path | None = None) -> list[str]:
    names = []
    for d in plugin_dirs(root):
        try:
            meta = yaml.safe_load((d / "plugin.yaml").read_text(encoding="utf-8"))
            name = meta["name"]
            if meta.get("collections"):
                services.records.load_yaml((d / meta["collections"]).read_text(encoding="utf-8"))
            if meta.get("tools"):
                spec = importlib.util.spec_from_file_location(f"forge_plugin_{name}", d / meta["tools"])
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)  # registers tools via @tool(..., source="plugin:<name>")
            names.append(name)
            log.info("loaded plugin %s from %s", name, d)
        except Exception:
            log.exception("failed to load plugin in %s", d)
    return names


def plugin_workflows(root: Path | None = None) -> list[Path]:
    out = []
    for d in plugin_dirs(root):
        meta = yaml.safe_load((d / "plugin.yaml").read_text(encoding="utf-8"))
        out += [d / w for w in meta.get("workflows", [])]
    return out
