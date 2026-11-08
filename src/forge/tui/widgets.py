"""Conversation, activity, and input widgets for the Forge terminal."""
from __future__ import annotations

import time

from rich.text import Text
from textual.containers import Vertical, VerticalScroll
from textual.message import Message
from textual.widgets import Markdown, OptionList, Static, TextArea
from textual.widgets.option_list import Option

from forge.tui.commands import Command, ToolMatch, matches


class StatusBar(Static):
    """Compact session header; detailed metrics live in /status and /work."""

    def render_status(self, st: dict | None, state: str, session_title: str, queued: int) -> None:
        title = session_title or "New conversation"
        model = ((st or {}).get("last_model_call") or {}).get("model", "Auto model")
        t = Text("Forge", style="bold", no_wrap=True, overflow="ellipsis")
        t.append(f"  ·  {title}\n{model}", style="dim")
        self.update(t)


class Speaker(Static):
    """Reply header ("● Forge"); left out of copied text."""
    ALLOW_SELECT = False


class Reply(Vertical):
    """One Forge reply. Uses Textual's Markdown (mouse-selectable, unlike a Rich Markdown renderable). Text that
    arrives before the widget is mounted is applied on mount; streamed text is appended, not re-parsed."""

    def __init__(self, text: str):
        super().__init__(classes="response")
        self.text = self._shown = text
        self._sync_pending = False

    def compose(self):
        self._shown = self.text
        yield Speaker(Text.from_markup("[green]●[/green] [bold]Forge[/bold]"))
        yield Markdown(self.text)

    def on_mount(self) -> None:
        self.call_after_refresh(self._schedule_sync)

    def show(self, text: str) -> None:
        self.text = text
        if self.is_mounted:
            self._schedule_sync()

    def _schedule_sync(self) -> None:
        if not self._sync_pending and self.text != self._shown:
            self._sync_pending = True
            self.run_worker(self._sync(), group="render-reply")

    async def _sync(self) -> None:
        try:
            md = self.query_one(Markdown)
            while self.text != self._shown:
                target = self.text
                if target.startswith(self._shown):
                    await md.append(target[len(self._shown):])
                else:
                    await md.update(target)
                self._shown = target
        finally:
            self._sync_pending = False


class Transcript(VerticalScroll):
    """A scrollable transcript with one mutable streaming response; all text can be selected with the mouse."""
    DEFAULT_CSS = """
    Transcript { height: 1fr; padding: 1 0; }
    Transcript > Static { height: auto; margin-bottom: 1; }
    Transcript > .response { height: auto; margin-bottom: 1; }
    Transcript > .response > Markdown { margin: 0; padding: 0; background: transparent; }
    Transcript > .response > Markdown > * { margin-top: 0; }
    """

    class PositionChanged(Message):
        def __init__(self, away_from_bottom: bool):
            super().__init__()
            self.away_from_bottom = away_from_bottom

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.pending: Reply | None = None
        self.responses: list[str] = []   # finished replies, oldest first (for /copy)
        self._last_away: bool | None = None

    def on_mount(self) -> None:
        # Textual's anchor follows layout changes, including deferred Markdown appends,
        # and releases itself when the user scrolls away from the bottom.
        self.anchor()
        self.call_after_refresh(self._publish_position)

    def _publish_position(self) -> None:
        away = self.max_scroll_y > 0 and not self.is_vertical_scroll_end
        if away != self._last_away:
            self._last_away = away
            self.post_message(self.PositionChanged(away))

    def watch_scroll_y(self, old_value: float, new_value: float) -> None:
        super().watch_scroll_y(old_value, new_value)
        if self.is_mounted:
            self._publish_position()

    def scroll_home(self, **kwargs) -> None:
        # Apply reading intent before pending response mounts can trigger another layout.
        kwargs.setdefault("immediate", True)
        super().scroll_home(**kwargs)

    def watch_virtual_size(self) -> None:
        if self.is_mounted:
            self.call_after_refresh(self._publish_position)

    def on_resize(self) -> None:
        self.call_after_refresh(self._publish_position)

    def write(self, content) -> None:
        self.mount(Static(content))

    def clear(self) -> None:
        self.pending = None
        self.responses.clear()
        self.remove_children()
        self.anchor()
        self.call_after_refresh(self._publish_position)

    def response(self, text: str, final: bool = False) -> None:
        if self.pending is None:
            self.pending = Reply(text)
            self.mount(self.pending)
        else:
            self.pending.show(text)
        if final:
            self.responses.append(text)
            self.pending = None


class ActivityIndicator(Static):
    """Event-driven activity with animation independent of server polling."""
    FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
    WORDS = ("Thinking", "Pondering", "Working")
    ACTIVE = {"thinking", "responding", "tool", "operation", "interrupting"}

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.state = "ready"
        self.label = ""
        self.started: float | None = None
        self.animations = True
        self.offline = False
        self.estop = False

    def on_mount(self) -> None:
        self.set_interval(0.1, self.tick)
        self.tick()

    def begin(self) -> None:
        self.started = time.monotonic()

    def set_state(self, state: str, label: str = "") -> None:
        if state in self.ACTIVE and self.started is None:
            self.begin()
        self.state, self.label = state, label
        self.tick()

    def tick(self) -> None:
        elapsed = max(0, time.monotonic() - self.started) if self.started is not None else 0
        state = "stopped" if self.estop else "offline" if self.offline else self.state
        active = state in self.ACTIVE
        labels = {"ready": "Ready", "responding": "Responding…", "approval": "Waiting for approval",
                  "interrupted": "Interrupted", "offline": "Connection lost · reconnecting…",
                  "stopped": "Emergency stop · /resume to continue", "error": "Something went wrong", "interrupting": "Stopping…"}
        if state == "thinking":
            word = self.WORDS[int(elapsed // 4) % len(self.WORDS)] if self.animations else "Thinking"
            label = word + "…"
        else:
            label = self.label if state in ("tool", "operation") else labels.get(state, state)
        colour = "red" if state in ("stopped", "error", "offline") else "yellow" if state in ("approval", "interrupted") else "cyan" if active else "green"
        icon = self.FRAMES[int(elapsed * 10) % len(self.FRAMES)] if active and self.animations else "●"
        out = Text()
        out.append(icon + " ", style=colour)
        out.append(label)
        if active:
            out.append(f"  {int(elapsed)}s", style="dim")
            if state != "operation":
                out.append("  ·  Ctrl+C interrupt", style="dim")
        self.update(out)


class Composer(TextArea):
    """Enter sends; Shift+Tab / Alt+Enter / Ctrl+J insert a newline. Arrows recall prompts at draft boundaries."""

    class Submitted(Message):
        def __init__(self, text: str) -> None:
            super().__init__()
            self.text = text

    class Interrupt(Message):
        pass

    class QuitRequested(Message):
        pass

    class ToolSelected(Message):
        def __init__(self, tool: ToolMatch) -> None:
            super().__init__()
            self.tool = tool

    def __init__(self, suggest: OptionList, **kw):
        super().__init__(**kw)
        self.suggest = suggest
        self.prompt_history: list[str] = []
        self._history_index: int | None = None
        self._history_draft = ""
        self._history_cursor = (0, 0)

    def set_prompt_history(self, prompts: list[str]) -> None:
        self.prompt_history = []
        for prompt in prompts:
            if prompt.strip() and (not self.prompt_history or self.prompt_history[-1] != prompt):
                self.prompt_history.append(prompt)
        self._history_index = None

    def remember_prompt(self, prompt: str) -> None:
        if prompt.strip() and (not self.prompt_history or self.prompt_history[-1] != prompt):
            self.prompt_history.append(prompt)
        self._history_index = None

    def _recall_prompt(self, direction: int) -> bool:
        if not self.prompt_history:
            return False
        if self._history_index is None:
            if direction > 0:
                return False
            self._history_draft, self._history_cursor = self.text, self.cursor_location
            self._history_index = len(self.prompt_history)
        self._history_index = min(len(self.prompt_history), max(0, self._history_index + direction))
        if self._history_index == len(self.prompt_history):
            self.text = self._history_draft
            self.move_cursor(self._history_cursor)
            self._history_index = None
        else:
            self.text = self.prompt_history[self._history_index]
            self.move_cursor(self.document.end)
        return True

    # -- autocomplete -------------------------------------------------------------------
    def _refresh_suggest(self) -> None:
        txt = self.text
        if self._history_index is not None and txt == self.prompt_history[self._history_index]:
            self.suggest.display = False
            return
        if txt.startswith("/") and " " not in txt and "\n" not in txt:
            cmds = matches(txt, getattr(self.app, "tools", []))
            self.suggest.clear_options()
            for c in cmds[:9]:
                label = Text()
                if isinstance(c, Command):
                    label.append("● ", style="cyan")
                    label.append(f"/{c.name}", style="bold")
                    label.append(f" {c.args}" if c.args else "", style="dim")
                    label.append(f"  {c.description}")
                    option_id = f"command:{c.name}"
                else:
                    label.append("● ", style="green" if c.enabled else "yellow")
                    label.append(c.name, style="bold")
                    label.append("  ")
                    label.append(c.description)
                    label.append("  enabled" if c.enabled else "  disabled", style="dim")
                    option_id = f"tool:{c.name}"
                self.suggest.add_option(Option(label, id=option_id))
            self.suggest.display = bool(cmds)
            if cmds:
                self.suggest.highlighted = 0
        else:
            self.suggest.display = False

    def on_text_area_changed(self, _event) -> None:
        self._fit_height()
        self._refresh_suggest()

    def _fit_height(self) -> None:
        self.styles.height = min(6, max(1, self.wrapped_document.height))

    def on_resize(self) -> None:
        self.call_after_refresh(self._fit_height)

    def _accept(self) -> bool:
        if not self.suggest.display or self.suggest.highlighted is None:
            return False
        opt = self.suggest.get_option_at_index(self.suggest.highlighted)
        if str(opt.id).startswith("tool:"):
            tool_name = str(opt.id)[5:]
            tool = next((t for t in matches(self.text, getattr(self.app, "tools", []))
                         if isinstance(t, ToolMatch) and t.name == tool_name), None)
            if tool:
                self.post_message(self.ToolSelected(tool))
                self.clear()
        else:
            command_name = str(opt.id).removeprefix("command:")
            self.text = f"/{command_name} "
            self.move_cursor(self.document.end)
        self.suggest.display = False
        return True

    async def _on_key(self, event) -> None:
        k = event.key
        if self.suggest.display and k in ("up", "down"):
            event.prevent_default()
            event.stop()
            n = self.suggest.option_count
            cur = self.suggest.highlighted or 0
            self.suggest.highlighted = (cur + (1 if k == "down" else -1)) % max(1, n)
            return
        if k in ("up", "down") and self.selection.is_empty:
            boundary = (self.navigator.is_first_wrapped_line(self.cursor_location) if k == "up"
                        else self.navigator.is_last_wrapped_line(self.cursor_location))
            if boundary and self._recall_prompt(-1 if k == "up" else 1):
                event.prevent_default()
                event.stop()
                return
        if k == "tab" and self.suggest.display:
            event.prevent_default()
            event.stop()
            self._accept()
            return
        if k == "escape" and self.suggest.display:
            event.prevent_default()
            event.stop()
            self.suggest.display = False
            return
        if k == "enter":
            event.prevent_default()
            event.stop()
            txt = self.text.strip()
            # Enter on a partial command name completes it; on a full command (or with args) it sends.
            if self.suggest.display and txt.startswith("/") and " " not in txt:
                opt = self.suggest.get_option_at_index(self.suggest.highlighted or 0)
                if str(opt.id).startswith("tool:"):
                    tool_name = str(opt.id)[5:]
                    tool = next((t for t in matches(txt, getattr(self.app, "tools", []))
                                 if isinstance(t, ToolMatch) and t.name == tool_name), None)
                    if tool:
                        self.post_message(self.ToolSelected(tool))
                        self.clear()
                    return
                if str(opt.id).removeprefix("command:") != txt[1:]:
                    self._accept()
                    return
            if txt:
                self.post_message(self.Submitted(self.text))
                self.clear()
            return
        if k in ("shift+tab", "ctrl+j", "alt+enter", "ctrl+n"):
            event.prevent_default()
            event.stop()
            self.insert("\n")
            return
        if k == "ctrl+c":
            event.prevent_default()
            event.stop()
            # Copy wins over clear/interrupt: text dragged in the transcript, then a selection in this box.
            picked = self.screen.get_selected_text() or self.selected_text
            if picked:
                self.app.copy_to_clipboard(picked)
                self.screen.clear_selection()
                self.app.notify(f"Copied {len(picked)} characters", timeout=2)
                return
            had_draft = bool(self.text)
            if had_draft:
                self.clear()
                self._history_index = None
            if not had_draft or getattr(self.app, "busy", False):
                self.post_message(self.Interrupt())
            return
        if k == "ctrl+d" and not self.text:
            event.prevent_default()
            event.stop()
            self.post_message(self.QuitRequested())
            return
        await super()._on_key(event)
