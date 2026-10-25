"""SQLite access. One connection per process guarded by a lock; WAL for concurrent readers (TUI, CLI)."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
import uuid
from importlib import resources
from pathlib import Path
from typing import Any, Iterable

from forge.config import forge_home


def now() -> float:
    return time.time()


def new_id(prefix: str = "") -> str:
    return f"{prefix}{uuid.uuid4().hex[:12]}"


def canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def sha(obj: Any) -> str:
    data = obj if isinstance(obj, str) else canonical(obj)
    return hashlib.sha256(data.encode()).hexdigest()


class DB:
    def __init__(self, path: Path | str | None = None):
        self.path = str(path or forge_home() / "forge.db")
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.execute("PRAGMA foreign_keys=ON")
        schema = resources.files("forge").joinpath("schema.sql").read_text(encoding="utf-8")
        with self._lock:
            self.conn.executescript(schema)
            from forge.migrations import migrate
            self.migration = migrate(self.conn, self.path)

    @property
    def schema_version(self) -> int:
        return self.one("PRAGMA user_version")["user_version"]

    def execute(self, sql: str, params: Iterable[Any] | dict = ()) -> sqlite3.Cursor:
        with self._lock:
            return self.conn.execute(sql, params if isinstance(params, dict) else tuple(params))

    def one(self, sql: str, params: Iterable[Any] = ()) -> dict | None:
        with self._lock:
            row = self.conn.execute(sql, tuple(params)).fetchone()
        return dict(row) if row else None

    def all(self, sql: str, params: Iterable[Any] = ()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self.conn.execute(sql, tuple(params)).fetchall()]

    def tx(self):
        return _Tx(self)

    # ---- kv -------------------------------------------------------------------
    def get_kv(self, key: str, default: str | None = None) -> str | None:
        row = self.one("SELECT value FROM kv WHERE key=?", (key,))
        return row["value"] if row else default

    def set_kv(self, key: str, value: str) -> None:
        self.execute("INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (key, value))

    # ---- audit (hash chain) -----------------------------------------------------
    def audit(self, actor: str, event: str, data: Any = None) -> None:
        with self._lock:
            prev = self.conn.execute("SELECT hash FROM audit ORDER BY id DESC LIMIT 1").fetchone()
            prev_hash = prev["hash"] if prev else "genesis"
            ts = now()
            data_json = canonical(data or {})
            h = sha(f"{prev_hash}|{ts}|{actor}|{event}|{data_json}")
            self.conn.execute("INSERT INTO audit(ts,actor,event,data_json,prev_hash,hash) VALUES(?,?,?,?,?,?)",
                              (ts, actor, event, data_json, prev_hash, h))

    def verify_audit_chain(self) -> tuple[bool, int | None]:
        prev_hash = "genesis"
        for r in self.all("SELECT * FROM audit ORDER BY id"):
            expect = sha(f"{prev_hash}|{r['ts']}|{r['actor']}|{r['event']}|{r['data_json']}")
            if r["prev_hash"] != prev_hash or r["hash"] != expect:
                return False, r["id"]
            prev_hash = r["hash"]
        return True, None


class _Tx:
    def __init__(self, db: DB):
        self.db = db

    def __enter__(self):
        self.db._lock.acquire()
        self.db.conn.execute("BEGIN IMMEDIATE")
        return self.db

    def __exit__(self, exc_type, *_):
        try:
            self.db.conn.execute("ROLLBACK" if exc_type else "COMMIT")
        finally:
            self.db._lock.release()
        return False
