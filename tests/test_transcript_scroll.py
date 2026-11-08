"""Sticky streaming, manual reading, and the floating return-to-bottom control."""
import pytest
from textual.widgets import Button

from forge.tui.widgets import Composer, Transcript
from tests.test_composer import app  # shared offline TUI fixture
from tests.test_tui_design import emit


@pytest.mark.parametrize("size", [(80, 24), (150, 45)])
async def test_stream_follows_bottom_and_button_preserves_manual_scroll(app, size):
    async with app.run_test(size=size) as pilot:
        app.session_id = "test"
        transcript = app.query_one(Transcript)
        controls = app.query_one("#scroll-controls")
        assert not controls.display
        for n in range(45):
            app.chat(f"Earlier message {n}")
        await pilot.pause()
        assert transcript.max_scroll_y > 0 and transcript.is_vertical_scroll_end
        viewport_height = transcript.size.height
        text = ""
        for n in range(4):
            text += f"\n\nStreaming paragraph {n}. " + "Text that wraps across the terminal. " * 8
            emit(app, "agent.delta", text=text[len(app._buf):])
            await pilot.pause()
            assert transcript.is_vertical_scroll_end
            assert not controls.display
        transcript.scroll_home(animate=False)
        await pilot.pause()
        assert transcript.scroll_y == 0 and controls.display
        assert transcript.size.height == viewport_height  # floating control does not steal transcript space
        emit(app, "agent.delta", text="\n\nMore streamed text. " * 20)
        await pilot.pause()
        assert transcript.scroll_y == 0 and controls.display
        button = app.query_one("#scroll-bottom", Button)
        assert button.region.bottom <= transcript.region.bottom
        await pilot.click("#scroll-bottom")
        await pilot.pause()
        assert transcript.is_vertical_scroll_end and not controls.display
        assert app.focused is app.query_one(Composer)
        emit(app, "agent.delta", text="\n\nAnother paragraph. " * 20)
        await pilot.pause()
        assert transcript.is_vertical_scroll_end
        emit(app, "agent.message", text=app._buf)
        await pilot.pause()
        assert transcript.is_vertical_scroll_end


async def test_scroll_up_cancels_pending_follow_and_scrolling_back_resumes_it(app):
    async with app.run_test(size=(80, 24)) as pilot:
        transcript = app.query_one(Transcript)
        for n in range(40):
            app.chat(f"Message {n}")
        await pilot.pause()
        transcript.response("A response that is still being mounted")
        transcript.scroll_home(animate=False)
        await pilot.pause()
        assert transcript.scroll_y == 0
        transcript.response("A response that is still being mounted\n\n" + "More text\n\n" * 25)
        await pilot.pause()
        assert transcript.scroll_y == 0
        transcript.scroll_to(y=transcript.max_scroll_y, animate=False)
        await pilot.pause()
        transcript.response("A replacement response\n\n" + "New text\n\n" * 40, final=True)
        await pilot.pause()
        assert transcript.is_vertical_scroll_end
        assert not app.query_one("#scroll-controls").display


async def test_new_chat_reset_and_new_prompt_restore_following(app):
    async with app.run_test(size=(80, 24)) as pilot:
        transcript = app.query_one(Transcript)
        for n in range(35):
            app.chat(f"Message {n}")
        await pilot.pause()
        transcript.scroll_home(animate=False)
        await pilot.pause()
        app.session_id = "test"
        await app._send_chat("New question")
        await pilot.pause()
        assert transcript.is_vertical_scroll_end
        transcript.scroll_home(animate=False)
        await pilot.pause()
        app._session_chosen("__new__")
        await pilot.pause()
        assert transcript.max_scroll_y == 0 and not app.query_one("#scroll-controls").display
        transcript.response("New chat\n\n" + "Streaming content\n\n" * 50)
        await pilot.pause()
        assert transcript.is_vertical_scroll_end
