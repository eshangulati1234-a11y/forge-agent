"""Spawns a real background daemon on a temp FORGE_HOME (Laya off), then drives the TUI against it headlessly."""
import json
import os
import socket

import pytest


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture(scope="module")
def daemon(tmp_path_factory):
    home = tmp_path_factory.mktemp("fh")
    old = os.environ.get("FORGE_HOME")
    os.environ["FORGE_HOME"] = str(home)
    from forge import launcher, settings
    from forge.config import init_home, reload_config
    init_home()
    reload_config()
    settings.set_value("daemon.port", str(_free_port()))
    settings.set_value("laya.enabled", "false")
    settings.set_value("notify_toast", "false")
    reload_config()
    st = launcher.ensure_daemon(timeout=60)
    yield st
    launcher.stop_daemon()
    if old is None:
        os.environ.pop("FORGE_HOME", None)
    else:
        os.environ["FORGE_HOME"] = old
    reload_config()


def test_daemon_spawned_detached(daemon):
    from forge import launcher
    assert daemon["ok"] and daemon["schema_version"] >= 2
    assert launcher.daemon_status() is not None


async def test_tui_autocomplete_settings_approval_work(daemon):
    from textual.widgets import OptionList

    from forge.client import SyncClient
    from forge.db import DB
    from forge.policy.approvals import Approvals
    from forge.tui import screens
    from forge.tui.app import ForgeTUI

    app = ForgeTUI()
    async with app.run_test(size=(150, 45)) as pilot:
        await pilot.pause(1.5)
        assert "Forge" in str(app.query_one("#status").render())
        # floating slash autocomplete
        await pilot.press("slash", "s", "e")
        await pilot.pause(0.3)
        sug = app.query_one("#suggest", OptionList)
        assert sug.display and {sug.get_option_at_index(i).id for i in range(sug.option_count)} >= {"command:settings", "command:sessions", "command:set"}
        await pilot.press("ctrl+c")                                         # clears the draft
        assert app.query_one("#composer").text == ""
        # settings overlay: edit a live setting through the UI
        await pilot.press("ctrl+o")
        await pilot.pause(1)
        assert isinstance(app.screen, screens.SettingsScreen)
        await app.screen.save("daemon.max_concurrent_runs", "3")
        await pilot.pause(0.3)
        vals = {s["key"]: s["value"] for s in SyncClient().get("/settings")["settings"]}
        assert vals["daemon.max_concurrent_runs"] == 3
        await pilot.press("escape")
        # approval pops up as a modal and is decided with one key
        ap = Approvals(DB()).request("action", "test: send hello", {"type": "action", "tool": "test.send", "args": {"x": 1}}, "test")
        app.handle_forge_event({"seq": 0, "ts": 0, "kind": "approval.requested", "approval_id": ap["id"], "title": "t"})
        await pilot.pause(1)
        assert isinstance(app.screen, screens.ApprovalScreen)
        await pilot.press("a")
        await pilot.pause(1)
        assert DB().one("SELECT status FROM approvals WHERE id=?", (ap["id"],))["status"] == "approved"
        # work monitor
        await pilot.press("ctrl+t")
        await pilot.pause(1)
        assert isinstance(app.screen, screens.WorkScreen)
        await pilot.press("escape")
        # other overlays open without errors
        for cmd, cls in (("/tools", screens.ToolsScreen), ("/doctor", screens.DoctorScreen), ("/help", screens.HelpScreen),
                         ("/model", screens.ModelsScreen)):
            await app.slash(cmd)
            await pilot.pause(1.5 if cmd == "/doctor" else 0.6)
            assert isinstance(app.screen, cls), cmd
            await pilot.press("escape")
            await pilot.pause(0.2)


def test_launchd_plist_has_a_usable_path_and_restarts_on_crash(monkeypatch):
    import plistlib

    from forge import launcher
    monkeypatch.setenv("PATH", "/usr/bin")
    d = plistlib.loads(launcher._launchd_plist().encode())
    path = d["EnvironmentVariables"]["PATH"].split(":")
    assert "/opt/homebrew/bin" in path and path.index("/opt/homebrew/bin") < path.index("/usr/bin")
    assert d["KeepAlive"] == {"SuccessfulExit": False} and d["RunAtLoad"] is True
    assert d["ProgramArguments"][1:] == ["-m", "forge.cli", "daemon"]


def test_startup_problem_detects_a_stale_interpreter(monkeypatch, tmp_path):
    import plistlib
    import sys

    from forge import launcher
    plist = tmp_path / "com.forge.daemon.plist"
    monkeypatch.setattr(launcher, "_plist_path", lambda: plist)
    assert launcher.startup_problem() == "not installed"
    plist.write_bytes(plistlib.dumps({"ProgramArguments": ["/no/such/python", "-m", "forge.cli", "daemon"]}))
    assert "missing interpreter" in launcher.startup_problem()
    plist.write_bytes(plistlib.dumps({"ProgramArguments": [sys.executable, "-m", "forge.cli", "daemon"]}))
    assert launcher.startup_problem() == ""


def test_port_pid_falls_back_to_lsof_when_psutil_is_denied(monkeypatch):
    import psutil

    from forge import launcher

    def denied(kind="tcp"):
        raise psutil.AccessDenied(1)
    monkeypatch.setattr(psutil, "net_connections", denied)
    monkeypatch.setattr(launcher.subprocess, "run", lambda *a, **k: type("R", (), {"stdout": "4242\n"})())
    assert launcher._port_pid(8765) == 4242
