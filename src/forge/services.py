"""Service container wired once per process (daemon, CLI, tests)."""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from forge.config import ForgeConfig, load_config
from forge.db import DB
from forge.events import EventBus
from forge.memory.store import MemoryStore
from forge.models.laya import LayaEngine
from forge.models.router import ModelRouter
from forge.notify import toast
from forge.policy.approvals import Approvals
from forge.policy.engine import PolicyEngine
from forge.records.store import RecordStore
from forge.tools.registry import REGISTRY, ToolRegistry

log = logging.getLogger("forge")


class Services:
    def __init__(self, cfg: ForgeConfig | None = None, db: DB | None = None, registry: ToolRegistry | None = None,
                 approval_key: bytes | None = None, laya: LayaEngine | None | bool = True):
        self.cfg = cfg or load_config()
        self.db = db or DB()
        self.events = EventBus()
        self.approvals = Approvals(self.db, self.events, key=approval_key)
        self.policy = PolicyEngine(self.db, self.approvals)
        self.registry = registry or REGISTRY
        self.router = ModelRouter(self.cfg, self.events)
        self.laya = LayaEngine(self.cfg.laya) if laya is True and self.cfg.laya.enabled else (laya or None)
        self.records = RecordStore(self.db, self.events)
        self.memory = MemoryStore(self.db, self.router)
        self.resources: dict[str, asyncio.Lock] = {"desktop": asyncio.Lock(), "browser": asyncio.Lock()}
        from forge.runtime.decisions import Decisions
        from forge.runtime.engine import Runtime
        from forge.runtime.repair import RepairEngine
        from forge.runtime.scheduler import Scheduler
        from forge.workflows.registry import WorkflowStore
        self.decisions = Decisions(self)
        self.runtime = Runtime(self)
        self.scheduler = Scheduler(self)
        self.workflows = WorkflowStore(self)
        self.repair = RepairEngine(self)
        self.plugins: list[str] = []

    def notify(self, title: str, body: str) -> None:
        self.events.emit("notify", title=title, body=body)
        if self.cfg.notify_toast:
            toast(title, body)

    def load_builtin_tools(self) -> None:
        import forge.tools.builtin  # noqa: F401  (registers on import)
        try:
            import forge.desktop.tools  # noqa: F401
        except Exception as e:
            log.warning("desktop tools unavailable: %s", e)
        try:
            import forge.browser.tools  # noqa: F401
        except Exception as e:
            log.warning("browser tools unavailable: %s", e)

    def load_plugins(self, root: Path | None = None) -> None:
        from forge.plugins import load_plugins
        self.plugins = load_plugins(self, root)
