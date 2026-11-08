import asyncio

from textual.app import App
from textual.widgets import Markdown
from textual.widgets._markdown import MarkdownOrderedList

from forge.tui.widgets import Reply
from tests.test_composer import app


async def test_fast_markdown_stream_renders_one_list():
    class Demo(App):
        def compose(self):
            yield Reply("Intro:")

    app = Demo()
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        reply = app.query_one(Reply)
        text = "Intro:"
        for char in "\n\n1. Open job links\n2. Create Excel file\n\nFirst, let me open":
            text += char
            reply.show(text)
            await asyncio.sleep(0)
        for _ in range(100):
            if reply._shown == text:
                break
            await pilot.pause(.01)
        markdown = reply.query_one(Markdown)
        assert markdown.source == text
        assert sum(isinstance(w, MarkdownOrderedList) for w in markdown.walk_children()) == 1
        reply.show("A corrected final answer")
        await pilot.pause()
        assert markdown.source == "A corrected final answer"


async def test_interrupt_clears_queue_before_request_and_ctrl_c_clears_active_draft(app, monkeypatch):
    from forge.tui.widgets import ActivityIndicator, Composer
    requested, release = asyncio.Event(), asyncio.Event()
    requests = []

    async def post(path, body=None, **kwargs):
        requests.append(path)
        assert not app.queue
        requested.set()
        await release.wait()
        return {"interrupted": True}

    monkeypatch.setattr(app.c, "post", post)
    async with app.run_test(size=(80, 24)) as pilot:
        app.session_id, app.busy = "test", True
        app.queue = ["Must not run after interrupt"]
        composer = app.query_one(Composer)
        composer.text = "Unsent draft"
        key = asyncio.create_task(pilot.press("ctrl+c"))
        try:
            await asyncio.wait_for(requested.wait(), 1)
            assert composer.text == "" and app._interrupt_pending
            assert app.query_one(ActivityIndicator).state == "interrupting"
            app.handle_forge_event({"kind": "agent.done", "session_id": "test", "ts": 0, "text": "(interrupted)"})
        finally:
            release.set()
        await key
        await pilot.pause()
        assert not app.busy and not app._interrupt_pending
        assert requests == ["/sessions/test/interrupt"]


async def test_reply_receiving_text_before_mount_does_not_duplicate_source():
    class Demo(App):
        pass

    app = Demo()
    async with app.run_test() as pilot:
        reply = Reply("Initial")
        app.mount(reply)
        reply.show("Initial and newer text")
        await pilot.pause()
        assert reply.query_one(Markdown).source == "Initial and newer text"
