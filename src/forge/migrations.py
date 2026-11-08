"""Versioned schema migrations (PRAGMA user_version).

schema.sql is the v1 baseline and is always applied with IF NOT EXISTS, so fresh installs and old installs
converge. Later changes are appended to MIGRATIONS and never edited once released. Before applying anything to an
existing database, forge.db is copied to backups/ so an update can always be rolled back by restoring the file.
"""
from __future__ import annotations

import datetime as dt
import logging
import shutil
import sqlite3
from pathlib import Path
from typing import Callable

log = logging.getLogger("forge.migrations")

Migration = tuple[int, str, "str | Callable[[sqlite3.Connection], None]"]

MIGRATIONS: list[Migration] = [
    (1, "baseline schema (schema.sql)", ""),
    (2, "index runs by workflow for the TUI run lists",
     "CREATE INDEX IF NOT EXISTS runs_workflow ON runs(workflow_id, created_at)"),
    (3, "messages folded into a session summary by /compact stay visible but leave the model context",
     "ALTER TABLE messages ADD COLUMN compacted INTEGER NOT NULL DEFAULT 0"),
]
LATEST = MIGRATIONS[-1][0]


def current_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def migrate(conn: sqlite3.Connection, db_path: str | None, migrations: list[Migration] | None = None,
            backup_dir: Path | None = None) -> dict:
    migs = migrations or MIGRATIONS
    latest = migs[-1][0]
    v = current_version(conn)
    fresh = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0 and \
        conn.execute("SELECT COUNT(*) FROM workflows").fetchone()[0] == 0 and \
        conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
    if v == 0:
        v = 1   # pre-migration installs already have the v1 tables from schema.sql
        conn.execute("PRAGMA user_version=1")
    pending = [m for m in migs if m[0] > v]
    if not pending:
        return {"from": v, "to": v, "applied": [], "backup": None}
    backup = None
    if db_path and db_path != ":memory:" and not fresh:
        bdir = backup_dir or Path(db_path).parent / "backups"
        bdir.mkdir(parents=True, exist_ok=True)
        backup = bdir / f"forge.db.v{v}.{dt.datetime.now():%Y%m%d-%H%M%S}"
        conn.execute("PRAGMA wal_checkpoint(FULL)")
        shutil.copy2(db_path, backup)
    applied = []
    for version, desc, step in pending:
        conn.execute("BEGIN IMMEDIATE")
        try:
            if callable(step):
                step(conn)
            elif step:
                # executescript() would COMMIT the open transaction, so statements run one by one inside it
                for stmt in (s.strip() for s in step.split(";")):
                    if stmt:
                        conn.execute(stmt)
            conn.execute(f"PRAGMA user_version={int(version)}")
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            log.exception("migration %d failed; database left at v%d (backup: %s)", version, current_version(conn), backup)
            raise
        applied.append(version)
        log.info("applied migration %d: %s", version, desc)
    return {"from": v, "to": latest, "applied": applied, "backup": str(backup) if backup else None}
