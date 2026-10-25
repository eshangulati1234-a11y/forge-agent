"""Triggers -> runs. Cron/interval schedules are persisted (next_fire_at) so restarts neither skip nor double-fire;
each fire carries a dedupe key `schedule:<fire_time>` that the runs table enforces as UNIQUE.
File triggers use watchdog; webhooks are handled by the daemon API.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import fnmatch
import json
import logging
import time
from pathlib import Path

from croniter import croniter

from forge.db import canonical, now
from forge.workflows.ir import Trigger, WorkflowIR

log = logging.getLogger("forge.scheduler")
MISFIRE_GRACE_S = 6 * 3600   # a missed fire within this window (e.g. machine asleep) runs once on wake


def next_fire(t: Trigger, after: float) -> float | None:
    if t.type == "cron" and t.cron:
        base = dt.datetime.fromtimestamp(after).astimezone()
        return croniter(t.cron, base).get_next(dt.datetime).timestamp()
    if t.type == "interval" and t.every_s:
        return after + t.every_s
    return None


class Scheduler:
    def __init__(self, services):
        self.s = services
        self.db = services.db
        self._observer = None

    def sync_workflow(self, ir: WorkflowIR) -> None:
        """Called on deploy: (re)create schedule rows for the deployed version's triggers."""
        self.db.execute("DELETE FROM schedules WHERE workflow_id=?", (ir.id,))
        for i, t in enumerate(ir.triggers):
            if t.type in ("cron", "interval", "file"):
                nf = next_fire(t, now())
                self.db.execute("INSERT INTO schedules(id,workflow_id,trigger_json,next_fire_at,enabled) VALUES(?,?,?,?,1)",
                                (f"{ir.id}:{i}", ir.id, t.model_dump_json(), nf))
        self._restart_watchers()

    def disable_workflow(self, workflow_id: str) -> None:
        self.db.execute("UPDATE schedules SET enabled=0 WHERE workflow_id=?", (workflow_id,))

    async def serve(self) -> None:
        self._restart_watchers()
        while True:
            try:
                self.tick()
            except Exception:
                log.exception("scheduler tick failed")
            await asyncio.sleep(self.s.cfg.daemon.scheduler_tick_seconds)

    def tick(self) -> None:
        t_now = now()
        for row in self.db.all("SELECT * FROM schedules WHERE enabled=1 AND next_fire_at IS NOT NULL AND next_fire_at<=?", (t_now,)):
            trig = Trigger.model_validate_json(row["trigger_json"])
            fire_at = row["next_fire_at"]
            if t_now - fire_at <= MISFIRE_GRACE_S:
                self.s.runtime.enqueue(row["workflow_id"], trigger=trig.type, inputs=trig.inputs,
                                       dedupe_key=f"{row['id']}@{int(fire_at)}")
            else:
                log.info("skipping stale fire of %s at %s", row["id"], fire_at)
            nf = next_fire(trig, max(t_now, fire_at))
            self.db.execute("UPDATE schedules SET next_fire_at=?, last_fired_at=? WHERE id=?", (nf, t_now, row["id"]))

    # ---- file triggers -------------------------------------------------------------------
    def _restart_watchers(self) -> None:
        try:
            from watchdog.events import FileSystemEventHandler
            from watchdog.observers import Observer
        except ImportError:
            return
        if self._observer:
            self._observer.stop()
            self._observer = None
        rows = [r for r in self.db.all("SELECT * FROM schedules WHERE enabled=1") if json.loads(r["trigger_json"])["type"] == "file"]
        if not rows:
            return
        sched = self

        class H(FileSystemEventHandler):
            def __init__(self, row, trig):
                self.row, self.trig = row, trig

            def on_created(self, event):
                if event.is_directory or not fnmatch.fnmatch(Path(event.src_path).name, self.trig.pattern):
                    return
                p = Path(event.src_path)
                time.sleep(0.5)  # let the writer finish
                mtime = int(p.stat().st_mtime) if p.exists() else 0
                sched.s.runtime.enqueue(self.row["workflow_id"], trigger="file", inputs={**self.trig.inputs, "path": str(p)},
                                        dedupe_key=f"{self.row['id']}@{p}@{mtime}")
            on_moved = lambda self, e: self.on_created(type("E", (), {"is_directory": e.is_directory, "src_path": e.dest_path})())

        obs = Observer()
        for r in rows:
            trig = Trigger.model_validate_json(r["trigger_json"])
            path = Path(trig.path).expanduser()
            path.mkdir(parents=True, exist_ok=True)
            obs.schedule(H(r, trig), str(path), recursive=False)
        obs.daemon = True
        obs.start()
        self._observer = obs
