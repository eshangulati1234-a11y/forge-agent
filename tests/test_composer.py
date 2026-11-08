"""Exercise composer keyboard behavior through Textual's real key dispatch."""
import pytest
from textual.widgets import OptionList

from forge.tui import app as tui
from forge.tui.widgets import Composer
from tests.test_tui_design import Client


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setattr(tui, "ForgeClient", Client)
    monkeypatch.setattr(tui.ForgeTUI, "listen", lambda self: None)
    monkeypatch.setattr(tui.ForgeTUI, "set_interval", lambda *args, **kwargs: None)
    return tui.ForgeTUI()


async def test_shift_tab_inserts_newline_without_sending_or_changing_focus(app):
    async with app.run_test(size=(80, 24)) as pilot:
        composer = app.query_one(Composer)
        composer.text = "firstsecond"
        composer.move_cursor((0, 5))
        await pilot.press("shift+tab")
        await pilot.pause()
        assert composer.text == "first\nsecond"
        assert composer.cursor_location == (1, 0)
        assert app.focused is composer
        assert app.c.posts == []
        await pilot.press("alt+enter", "ctrl+j")
        assert composer.text == "first\n\n\nsecond"


async def test_arrows_recall_submitted_prompts_and_restore_draft_cursor(app):
    async with app.run_test(size=(80, 24)) as pilot:
        composer = app.query_one(Composer)
        app.session_id, app.busy = "test", True
        for prompt in ("first prompt", "second prompt"):
            composer.text = prompt
            composer.move_cursor(composer.document.end)
            await pilot.press("enter")
            await pilot.pause()
        assert app.queue == ["first prompt", "second prompt"]
        composer.text = "unfinished draft"
        composer.move_cursor((0, 4))
        await pilot.press("up")
        assert composer.text == "second prompt"
        await pilot.press("up", "up")
        assert composer.text == "first prompt"
        await pilot.press("down")
        assert composer.text == "second prompt"
        await pilot.press("down")
        assert composer.text == "unfinished draft" and composer.cursor_location == (0, 4)
        await pilot.press("down")
        assert composer.text == "unfinished draft"
        await pilot.press("up")
        composer.insert(" edited")
        await pilot.press("enter")
        await pilot.pause()
        assert composer.prompt_history == ["first prompt", "second prompt", "second prompt edited"]
        assert app.queue[-1] == "second prompt edited"


async def test_multiline_and_wrapped_drafts_keep_normal_arrow_navigation(app):
    async with app.run_test(size=(60, 24)) as pilot:
        composer = app.query_one(Composer)
        composer.set_prompt_history(["older prompt"])
        composer.text = "first\nsecond\nthird"
        composer.move_cursor((1, 2))
        await pilot.press("up")
        assert composer.text == "first\nsecond\nthird" and composer.cursor_location[0] == 0
        await pilot.press("down")
        assert composer.cursor_location[0] == 1
        composer.text = "long draft " * 20
        composer.move_cursor(composer.document.end)
        await pilot.pause()
        assert composer.wrapped_document.height > 1
        await pilot.press("up")
        assert composer.text.startswith("long draft ")
        assert composer.cursor_location != composer.document.end


async def test_history_commands_do_not_compete_with_autocomplete(app):
    async with app.run_test(size=(80, 24)) as pilot:
        composer = app.query_one(Composer)
        composer.set_prompt_history(["older prompt", "/status"])
        await pilot.press("up")
        await pilot.pause()
        assert composer.text == "/status" and not composer.suggest.display
        await pilot.press("up")
        assert composer.text == "older prompt"
        await pilot.press("ctrl+c")
        composer.text = "/se"
        await pilot.pause()
        options = app.query_one("#suggest", OptionList)
        assert options.display
        selected = options.highlighted
        await pilot.press("down")
        assert options.highlighted != selected and composer.text == "/se"
