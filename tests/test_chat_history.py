"""Saved-chat discovery and reopening, using the real SQLite transcript store."""
from types import SimpleNamespace

import httpx
import pytest
from textual.widgets import Input, OptionList, TabbedContent

from forge.agent.loop import Agent
from forge.client import ClientError, read_chat
from forge.db import DB
from forge.tui import app as tui
from forge.tui.commands import BY_NAME, matches
from forge.tui.screens import SessionsScreen
from forge.tui.widgets import Composer, Transcript
from tests.test_tui_design import Client


@pytest.fixture
def saved(services):
    agent = Agent(services)
    oldest = agent.new_session("Earlier job applications")
    for n in range(205):
        agent._save(oldest, "user" if n % 2 == 0 else "assistant", f"Saved message {n}")
    services.db.execute("UPDATE sessions SET updated_at=1 WHERE id=?", (oldest,))
    for n in range(60):
        sid = agent.new_session(f"Chat {n}")
        services.db.execute("UPDATE sessions SET updated_at=? WHERE id=?", (n + 2, sid))
    return agent, oldest


def test_saved_history_survives_new_connection_and_pages(saved, services):
    _, oldest = saved
    db = DB(services.db.path)
    try:
        agent = Agent(SimpleNamespace(db=db))
        assert len(agent.sessions()) == 30
        assert len(agent.sessions(limit=200)) == 61
        assert agent.sessions(limit=50, offset=50)[-1]["id"] == oldest
        assert agent.sessions(q="JOB APPLICATIONS")[0]["id"] == oldest
        assert agent.sessions(q=oldest)[0]["id"] == oldest
        assert agent.sessions(q="%") == []  # search is literal, not a SQL wildcard
        recent = agent.history(oldest)
        earlier = agent.history(oldest, before_id=recent[0]["id"])
        assert len(recent) == 200 and len(earlier) == 5
        assert (earlier + recent)[0]["content"] == "Saved message 0"
        assert (earlier + recent)[-1]["content"] == "Saved message 204"
    finally:
        db.conn.close()


async def test_history_api_pagination_metadata_and_validation(saved, services, monkeypatch):
    from forge.config import api_token
    from forge.daemon import app as daemon
    agent, oldest = saved
    monkeypatch.setattr(daemon, "S", services)
    monkeypatch.setattr(daemon, "AGENT", agent)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=daemon.app), base_url="http://test",
                                headers={"Authorization": f"Bearer {api_token()}"}) as client:
        rows = (await client.get("/sessions", params={"limit": 51, "offset": 10})).json()
        assert len(rows) == 51 and rows[-1]["id"] == oldest
        assert (await client.get(f"/sessions/{oldest}")).json()["title"] == "Earlier job applications"
        assert (await client.get("/sessions/missing")).status_code == 404
        assert (await client.get("/sessions", params={"offset": -1})).status_code == 422
        recent = (await client.get(f"/sessions/{oldest}/messages", params={"limit": 200})).json()
        earlier = (await client.get(f"/sessions/{oldest}/messages", params={"before_id": recent[0]["id"]})).json()
        assert len(earlier) == 5


@pytest.fixture
def history_app(saved, monkeypatch):
    agent, oldest = saved

    class HistoryClient(Client):
        async def get(self, path, **kwargs):
            if path == "/sessions":
                return agent.sessions(**kwargs)
            if path.startswith("/sessions/"):
                sid = path.split("/")[2]
                if path.endswith("/messages"):
                    return agent.history(sid, **kwargs)
                row = agent.s.db.one("SELECT * FROM sessions WHERE id=?", (sid,))
                if row:
                    return row
                raise ClientError("404: no session")
            return await super().get(path, **kwargs)

    monkeypatch.setattr(tui, "ForgeClient", HistoryClient)
    monkeypatch.setattr(tui.ForgeTUI, "listen", lambda self: None)
    # Periodic daemon refresh is not part of these deterministic navigation tests.
    monkeypatch.setattr(tui.ForgeTUI, "set_interval", lambda *args, **kwargs: None)
    return tui.ForgeTUI(), oldest


async def test_history_picker_search_paging_and_resume(history_app):
    app, oldest = history_app
    async with app.run_test(size=(100, 35)) as pilot:
        await app.slash("/history")
        await pilot.pause()
        assert isinstance(app.screen, SessionsScreen)
        assert app.screen.has_more
        await pilot.press("ctrl+right")
        await pilot.pause()
        assert app.screen.page_offset == 50
        assert app.screen.query_one("#sess", OptionList).option_count == 11
        await pilot.press("ctrl+left")
        await pilot.pause()
        assert app.screen.page_offset == 0
        app.screen.query_one("#filter", Input).value = "Earlier job"
        await pilot.pause()
        assert app.screen.query_one("#sess", OptionList).option_count == 1
        app.queue = ["Don't send this in the resumed chat"]
        await pilot.press("enter")
        await pilot.pause()
        assert app.session_id == oldest and app.session_title == "Earlier job applications"
        assert not app.queue
        assert len(app.query_one(Transcript).children) == 206  # all messages, plus session marker
        assert app.query_one(Composer).prompt_history[-1] == "Saved message 204"
        assert app.query_one(TabbedContent).active == "chat"
        assert not app.c.posts


async def test_resume_search_cancel_and_emergency_stop_stay_separate(history_app):
    app, oldest = history_app
    async with app.run_test(size=(100, 35)) as pilot:
        await app.slash(f"/resume {oldest}")
        assert app.session_id == oldest
        await app.slash("/resume Chat")  # ambiguous search opens picker rather than choosing silently
        await pilot.pause()
        assert isinstance(app.screen, SessionsScreen)
        await pilot.press("escape")
        assert app.session_id == oldest
        app.last_status["emergency_stop"] = True
        await app.slash("/resume")
        await pilot.pause()
        assert isinstance(app.screen, SessionsScreen)
        assert app.last_status["emergency_stop"]
        assert not app.c.posts
        await pilot.press("escape")
        await app.slash("/unstop")
        assert app.c.posts == [("/resume", None)]
        await app.slash("/new")
        assert app.session_id is None
        assert app.query_one(Composer).prompt_history == []
        await app.slash("/resume Earlier job")
        assert app.session_id == oldest
        await app._send_chat("Continue this conversation")
        assert app.c.posts[-1] == (f"/sessions/{oldest}/chat", {"text": "Continue this conversation"})
        await app._resume_session("missing")
        assert app.session_id == oldest


def test_history_commands_are_discoverable():
    assert BY_NAME["resume"].category == "Session"
    assert BY_NAME["unstop"].category == "Safety"
    assert any(c.name == "history" for c in matches("/hist"))


@pytest.mark.parametrize("outside_list", [False, True])
async def test_resume_with_older_daemon_missing_details_and_ignoring_pagination(saved, outside_list):
    agent, oldest = saved

    class OldDaemon:
        def __init__(self):
            self.limits = []

        async def get(self, path, **params):
            if path == "/sessions":
                return agent.sessions(limit=30 if outside_list else 200)
            if path == f"/sessions/{oldest}/messages":
                self.limits.append(params["limit"])
                return agent.history(oldest, params["limit"])  # before_id is silently ignored
            if path == f"/sessions/{oldest}/context":
                return {"session_id": oldest, "busy": False}
            raise ClientError("404: Not Found")

    client = OldDaemon()
    session, messages = await read_chat(client, oldest)
    assert session["id"] == oldest
    assert session["title"] == ("Saved message 0" if outside_list else "Earlier job applications")
    assert len(messages) == 205 and messages[0]["content"] == "Saved message 0"
    assert messages[-1]["content"] == "Saved message 204"
    assert client.limits == [200, 400]
    with pytest.raises(ClientError, match="404"):
        await read_chat(client, "missing")


async def test_resume_propagates_non_404_errors():
    class Offline:
        async def get(self, path, **params):
            raise ClientError("503: temporarily unavailable")

    with pytest.raises(ClientError, match="503"):
        await read_chat(Offline(), "anything")
