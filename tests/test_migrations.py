import sqlite3
from importlib import resources

import pytest

from forge.db import DB
from forge.migrations import LATEST, migrate


def test_fresh_db_is_latest_without_backup(tmp_path):
    db = DB(tmp_path / "f.db")
    assert db.schema_version == LATEST
    assert db.migration["backup"] is None
    assert not (tmp_path / "backups").exists()


def test_old_install_migrates_with_backup_and_keeps_data(tmp_path):
    p = tmp_path / "old.db"
    conn = sqlite3.connect(p)
    conn.executescript(resources.files("forge").joinpath("schema.sql").read_text(encoding="utf-8"))
    conn.execute("INSERT INTO workflows(id,description,created_at) VALUES('w.x','keep me',1)")
    conn.commit()
    conn.close()                               # user_version 0: a pre-migrations install
    db = DB(p)
    assert db.schema_version == LATEST
    assert db.migration["applied"] and db.migration["backup"]
    assert db.one("SELECT description FROM workflows WHERE id='w.x'")["description"] == "keep me"
    assert db.one("SELECT name FROM sqlite_master WHERE name='runs_workflow'")
    again = DB(p)                              # idempotent
    assert again.migration["applied"] == []


def test_failed_migration_rolls_back(tmp_path):
    p = tmp_path / "x.db"
    DB(p)
    conn = sqlite3.connect(p, isolation_level=None)
    bad = [(1, "base", ""), (2, "ok", "SELECT 1"), (LATEST + 1, "boom", "CREATE TABLE broken (")]
    with pytest.raises(sqlite3.OperationalError):
        migrate(conn, str(p), bad)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == LATEST
