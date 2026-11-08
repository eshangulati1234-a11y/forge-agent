"""Drive the terminal UI with deterministic events, without a model or daemon."""
from pathlib import Path
from dataclasses import asdict

import pytest
from textual.widgets import OptionList, Static, TabbedContent

from forge.tui import app as tui
from forge.tui.screens import ApprovalScreen
from forge.config import ForgeConfig
from forge.settings import describe
from forge.tui.widgets import ActivityIndicator, Composer, Transcript


class Client:
    def __init__(self):
        self.posts = []

    async def get(self, path, **kwargs):
        if path == "/settings":
            values = [asdict(s) for s in describe(ForgeConfig()) if s.key.startswith("tui.")]
            next(s for s in values if s["key"] == "tui.theme")["value"] = "textual-dark"
            return {"settings": values}
        if path == "/status":
            return {"last_model_call": {"model": "local-model"}, "active_runs": []}
        if path == "/tools":
            return [{"name": "records.query", "description": "Find saved records", "enabled": True,
                     "source": "builtin", "side_effect": "none", "caps": []}]
        if path == "/models/available":
            return {"capabilities": [], "pins": {}, "nodes": []}
        if path == "/work":
            return {"agent_turns": [], "desktop_lease": None, "queued": 0,
                    "runs": [], "waiting": [], "model_calls": []}
        return []

    async def post(self, path, body=None, **kwargs):
        self.posts.append((path, body))
        return {"session_id": "test"}


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setattr(tui, "ForgeClient", Client)
    monkeypatch.setattr(tui.ForgeTUI, "listen", lambda self: None)
    monkeypatch.setattr(tui, "local_windows", lambda: False)   # never touch the real clipboard from tests
    return tui.ForgeTUI()


def emit(app, kind, **values):
    app.handle_forge_event({"kind": kind, "session_id": "test", "ts": 0, **values})


async def test_activity_stream_tools_and_terminal_states(app):
    async with app.run_test(size=(80, 24)) as pilot:
        app.session_id = "test"
        activity = app.query_one(ActivityIndicator)
        transcript = app.query_one(Transcript)
        emit(app, "agent.thinking")
        assert activity.state == "thinking"
        started = activity.started
        activity.started -= 4
        activity.tick()
        assert "Pondering" in str(activity.render())
        activity.animations = False
        activity.tick()
        assert "Thinking" in str(activity.render())
        assert str(activity.render()).startswith("●")
        emit(app, "agent.delta", text="Hello **world**")
        await pilot.pause()
        pending = transcript.pending
        emit(app, "agent.delta", text="\n\n- one\n- two")
        emit(app, "agent.message", text="Hello **world**\n\n- one\n- two")
        await pilot.pause()
        assert transcript.pending is None
        assert pending in transcript.children
        assert len(transcript.children) == 2  # welcome plus exactly one response
        emit(app, "tool.start", tool="records.query", call_id="one")
        emit(app, "tool.start", tool="fs.read", call_id="two")
        assert "2 active" in str(activity.render())
        emit(app, "tool.end", tool="records.query", call_id="one", ms=100)
        assert "fs.read" in str(activity.render())
        emit(app, "tool.error", tool="fs.read", call_id="two", error="missing")
        assert not app.active_tools
        emit(app, "agent.waiting_approval")
        assert "Waiting for approval" in str(activity.render())
        app._event_connected = False
        app.render_status()
        assert "Connection lost" in str(activity.render())
        app._event_connected = True
        app.render_status()
        assert activity.state == "approval"
        app.last_status["emergency_stop"] = True
        app.render_status()
        assert "Emergency stop" in str(activity.render())
        app.last_status["emergency_stop"] = False
        emit(app, "agent.done", text="(interrupted)")
        assert activity.state == "interrupted" and not app.busy
        assert activity.started <= started
        app._turn_message = False
        emit(app, "agent.thinking")
        emit(app, "agent.done", text="I hit an error I can't work around: offline")
        assert activity.state == "error"
        app.handle_forge_event({"kind": "agent.thinking", "session_id": "another", "ts": 0})
        assert activity.state == "error"
        emit(app, "agent.thinking")
        emit(app, "agent.done", text="Finished")
        assert activity.state == "ready"


async def test_commands_navigation_queue_and_scroll(app):
    async with app.run_test(size=(80, 24)) as pilot:
        assert app.theme == "forge-black"
        composer = app.query_one(Composer)
        composer.text = "/records.query"
        await pilot.pause()
        assert app.query_one(OptionList).option_count == 1
        await pilot.press("enter")
        await pilot.pause()
        assert app.c.posts == []  # tool selection only explains
        composer.text = "/se"
        await pilot.pause()
        await pilot.press("tab")
        assert composer.text.startswith("/sessions ")
        await pilot.press("ctrl+c")
        for view in ("runs", "workflows", "approvals", "records", "memory", "activity"):
            await app.slash("/" + view)
            await pilot.pause()
            assert app.query_one(TabbedContent).active == view
            await pilot.press("escape")
            assert app.query_one(TabbedContent).active == "chat"
            assert app.focused is composer
        app.session_id = "test"
        emit(app, "agent.thinking")
        app._submitted(Composer.Submitted("next task"))
        assert app.queue == ["next task"]
        assert app.query_one(ActivityIndicator).state == "thinking"
        emit(app, "agent.done", text="Done")
        await pilot.pause()
        assert app.c.posts[-1] == ("/sessions/test/chat", {"text": "next task"})
        transcript = app.query_one(Transcript)
        for n in range(40):
            app.chat(f"Message {n}")
        await pilot.pause()
        transcript.scroll_home(animate=False)
        await pilot.pause()
        transcript.response("A streaming response")
        await pilot.pause()
        assert transcript.scroll_y == 0


@pytest.mark.parametrize("size", [(80, 24), (150, 45)])
async def test_layout_and_overlay_snapshots(app, size):
    async with app.run_test(size=size) as pilot:
        app.session_id = "test"
        app.chat("[bold]› Find my saved records[/bold]")
        emit(app, "agent.thinking")
        emit(app, "agent.delta", text="Here are your **saved records**.\n\n- First record\n- Second record")
        await pilot.pause()
        composer = app.query_one(Composer)
        assert composer.region.bottom < size[1]
        assert app.query_one(Transcript).size.height >= 8
        composer.text = "one\ntwo\nthree\nfour\nfive\nsix\nseven"
        await pilot.pause()
        assert composer.size.height == 6
        composer.clear()
        await pilot.pause()
        out = Path(".pytest_cache/tui-previews")
        out.mkdir(parents=True, exist_ok=True)
        app.save_screenshot(f"chat-{size[0]}.svg", str(out))
        for screen in ("help", "settings", "models", "tools", "sessions", "doctor", "work"):
            app.action_open(screen)
            await pilot.pause()
            modal = app.screen.query_one(".modal")
            assert modal.region.right <= size[0]
            assert modal.region.bottom <= size[1]
            app.save_screenshot(f"{screen}-{size[0]}.svg", str(out))
            await pilot.press("escape")
            await pilot.pause()
            assert app.focused is composer
        app.push_screen(ApprovalScreen({"id": "approval", "title": "Write exported records",
                                      "kind": "action", "subject_json": '{"tool": "excel.export"}'}))
        await pilot.pause()
        app.save_screenshot(f"approval-{size[0]}.svg", str(out))
        await pilot.press("escape")
        for view in ("runs", "workflows", "approvals", "records", "memory", "activity"):
            app.action_tab(view)
            await pilot.pause()
            app.save_screenshot(f"{view}-{size[0]}.svg", str(out))


CTX = {"session_id": "test", "model": "qwen3.5:9b", "window": 16384, "used": 12000, "remaining": 4384, "measured_at": 0,
       "history_messages": 40, "history_tokens": 11000, "history_budget": 5000, "sent_messages": 14, "dropped_messages": 26,
       "summary": False, "compacted_messages": 0, "can_compact": True, "busy": False}
LOADED = [{"node": "local", "name": "qwen3.5:9b", "size_mb": 7400, "vram_mb": 7400, "ctx": 16384,
           "expires_at": "2999-01-01T00:00:00.123456789+11:00"}]


def test_status_report_shows_context_models_and_compact_option():
    st = {"gpu": {"mem_used_mb": 9800, "mem_total_mb": 12288, "util_pct": 4, "name": "RTX"}, "uptime_s": 3700,
          "active_runs": [], "pending_approvals": 1}
    out = tui.status_markup(st, CTX, LOADED, "jobs")
    assert "12.0k of 16.4k used" in out and "4.4k left" in out and "26 older not sent" in out
    assert "qwen3.5:9b  7.2 GB VRAM · ctx 16384 · stays loaded" in out
    assert "9.6 of 12.0 GB used" in out and "1 approvals pending" in out
    assert "[@click=app.compact]" in out
    est = tui.status_markup(st, {**CTX, "used": None, "estimate": 6200}, LOADED)
    assert "~6.2k of 16.4k used" in est and "estimate for the next call" in est
    assert "@click" not in tui.status_markup(st, {**CTX, "busy": True}, LOADED)
    assert "none loaded" in tui.status_markup(st, None, [])
    assert "Ollama unreachable" in tui.status_markup(st, None, [{"node": "local", "error": "refused"}])


async def test_status_and_compact_commands(app):
    async def get(path, **kw):
        if path == "/sessions/test/context":
            return {**CTX, "loaded_models": LOADED}
        return await Client.get(app.c, path, **kw)

    async def post(path, body=None, **kw):
        app.c.posts.append((path, body))
        return {"compacted": 34, "summary": "Goal: AI jobs", "history_tokens_before": 11000, "history_tokens_after": 1500}

    async with app.run_test(size=(100, 30)) as pilot:
        app.session_id = "test"
        app.c.get, app.c.post = get, post
        await app.slash("/status")
        await pilot.pause()
        report = app.query_one(Transcript).children[-1]
        assert "4.4k left" in str(report.render())
        await app.action_compact()
        await pilot.pause()
        assert app.c.posts[-1][0] == "/sessions/test/compact"
        assert "Goal: AI jobs" in str(app.query_one(Transcript).children[-1].render())


async def test_replies_are_selectable_and_ctrl_c_copies_instead_of_interrupting(app):
    from textual.selection import Selection
    async with app.run_test(size=(100, 30)) as pilot:
        app.session_id = "test"
        transcript = app.query_one(Transcript)
        emit(app, "agent.delta", text="Applied for **Maincode**")
        emit(app, "agent.message", text="Applied for **Maincode**\n\n- name\n- email")
        await pilot.pause()
        composer = app.query_one(Composer)
        composer.focus()
        composer.insert("draft")
        await pilot.pause()
        await pilot.pause()
        reply = transcript.children[-1]
        app.screen.selections = {w: Selection(None, None) for w in reply.query("*") if w.allow_select}
        await pilot.press("ctrl+c")
        assert app.clipboard.startswith("Applied for Maincode") and "email" in app.clipboard   # header not copied
        assert composer.text == "draft" and not app.screen.selections        # copied; draft kept, nothing interrupted
        await pilot.press("ctrl+c")
        assert composer.text == ""                                           # no selection: old behaviour
        assert not [p for p in app.c.posts if p[0].endswith("/interrupt")]


async def test_copy_command_copies_recent_replies(app):
    async with app.run_test(size=(100, 30)) as pilot:
        app.session_id = "test"
        await app.slash("/copy")
        emit(app, "agent.message", text="first **reply**")
        emit(app, "agent.message", text="second reply")
        await pilot.pause()
        await app.slash("/copy")
        assert app.clipboard == "second reply"
        await app.slash("/copy 2")
        assert app.clipboard == "first **reply**"   # the markdown source, ready to paste
