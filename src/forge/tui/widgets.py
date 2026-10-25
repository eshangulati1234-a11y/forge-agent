"""TUI widgets: Hermes-style status bar and the composer with floating slash-command autocomplete."""
from __future__ import annotations

from rich.text import Text
from textual.message import Message
from textual.widgets import OptionList, Static, TextArea
from textual.widgets.option_list import Option

from forge.tui.commands import matches


def _ctx_colour(frac: float) -> str:
    return "green" if frac < 0.5 else "yellow" if frac < 0.8 else "dark_orange" if frac < 0.95 else "red"


class StatusBar(Static):
    """state · model@node · context bar · session · GPU · Laya · runs · approvals · badges. Width-responsive."""

    def render_status(self, st: dict | None, state: str, session_title: str, queued: int) -> None:
        if st is None:
            self.update(Text(" ● forged unreachable — run `forge` to start it", style="bold red"))
            return
        w = self.size.width or 120
        t = Text()
        if st.get("emergency_stop"):
            t.append(" ⛔ E-STOP ", style="bold white on red")
            t.append(" ")
        icon = {"ready": ("●", "green"), "thinking": ("◐", "cyan"), "tool": ("▶", "magenta"), "approval": ("⚠", "yellow"),
                "interrupted": ("■", "red"), "queued": ("…", "cyan")}.get(state.split(":")[0], ("●", "white"))
        t.append(f"{icon[0]} {state}", style=f"bold {icon[1]}")
        lc = st.get("last_model_call") or {}
        if lc and w >= 60:
            t.append("  │ ", style="dim")
            t.append(f"{lc.get('model', '?')}@{lc.get('node', '?')}", style="bold")
            ctx, used = lc.get("ctx") or 0, lc.get("context_tokens") or 0
            if ctx:
                frac = min(1.0, used / ctx)
                bar = int(frac * 10)
                t.append("  ")
                t.append("█" * bar + "░" * (10 - bar), style=_ctx_colour(frac))
                t.append(f" {used // 1000}k/{ctx // 1000}k", style=_ctx_colour(frac))
        g = st.get("gpu") or {}
        if g.get("mem_total_mb") and w >= 100:
            t.append("  │ ", style="dim")
            t.append(f"GPU {g['mem_used_mb'] / 1024:.1f}/{g['mem_total_mb'] / 1024:.0f}G {g.get('util_pct', 0)}%")
        lay = st.get("laya") or {}
        if w >= 100:
            t.append("  │ ", style="dim")
            t.append("Laya ", style="")
            t.append("●" if lay.get("loaded") else "○", style="green" if lay.get("loaded") else "dim")
        runs = st.get("runs") or {}
        active = len(st.get("active_runs") or [])
        t.append("  │ ", style="dim")
        t.append(f"▶ {active}", style="magenta" if active else "dim")
        if runs.get("queued"):
            t.append(f" +{runs['queued']}q", style="cyan")
        if queued:
            t.append(f"  [{queued} queued msg]", style="cyan")
        if st.get("pending_approvals"):
            t.append(f"  ⚠ {st['pending_approvals']} approval{'s' if st['pending_approvals'] > 1 else ''}", style="bold yellow")
        if st.get("restart_required"):
            t.append("  ↻ restart needed", style="yellow")
        if session_title and w >= 80:
            title = session_title[:28]
            pad = max(1, w - len(t.plain) - len(title) - 4)
            t.append(" " * pad)
            t.append(f" {title} ", style="reverse")
        self.update(t)


class Composer(TextArea):
    """Multi-line input. Enter sends; Alt+Enter / Ctrl+J newline; '/' opens floating autocomplete."""

    class Submitted(Message):
        def __init__(self, text: str) -> None:
            super().__init__()
            self.text = text

    class Interrupt(Message):
        pass

    class QuitRequested(Message):
        pass

    def __init__(self, suggest: OptionList, **kw):
        super().__init__(**kw)
        self.suggest = suggest

    # -- autocomplete -------------------------------------------------------------------
    def _refresh_suggest(self) -> None:
        txt = self.text
        if txt.startswith("/") and " " not in txt and "\n" not in txt:
            cmds = matches(txt)
            self.suggest.clear_options()
            for c in cmds[:9]:
                label = Text()
                label.append(f"/{c.name}", style="bold cyan")
                label.append(f" {c.args}" if c.args else "", style="dim")
                label.append(f"  {c.description}")
                self.suggest.add_option(Option(label, id=c.name))
            self.suggest.display = bool(cmds)
            if cmds:
                self.suggest.highlighted = 0
        else:
            self.suggest.display = False

    def on_text_area_changed(self, _event) -> None:
        self._refresh_suggest()

    def _accept(self) -> bool:
        if not self.suggest.display or self.suggest.highlighted is None:
            return False
        opt = self.suggest.get_option_at_index(self.suggest.highlighted)
        self.text = f"/{opt.id} "
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
                if opt.id != txt[1:]:
                    self._accept()
                    return
            if txt:
                self.post_message(self.Submitted(self.text))
                self.clear()
            return
        if k in ("ctrl+j", "alt+enter", "ctrl+n"):
            event.prevent_default()
            event.stop()
            self.insert("\n")
            return
        if k == "ctrl+c":
            event.prevent_default()
            event.stop()
            if self.text:
                self.clear()
            else:
                self.post_message(self.Interrupt())
            return
        if k == "ctrl+d" and not self.text:
            event.prevent_default()
            event.stop()
            self.post_message(self.QuitRequested())
            return
        await super()._on_key(event)
