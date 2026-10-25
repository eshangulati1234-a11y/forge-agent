import os
import sys
from pathlib import Path

import pytest


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("FORGE_HOME", str(tmp_path / "forge_home"))
    from forge import config
    config.load_config.cache_clear()
    return tmp_path / "forge_home"


@pytest.fixture
def services(home):
    from forge.config import load_config
    from forge.db import DB
    from forge.services import Services
    cfg = load_config()
    cfg.notify_toast = False
    cfg.daemon.scheduler_tick_seconds = 0.2
    s = Services(cfg=cfg, db=DB(home / "forge.db"), approval_key=b"k" * 32, laya=False)
    s.load_builtin_tools()
    import tests.helpers  # noqa: F401  registers test.* tools
    return s


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
