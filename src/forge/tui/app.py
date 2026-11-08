"""Forge terminal UI (Textual) — a pure client of forged (HTTP commands + WebSocket event stream).
Works over SSH; quitting never affects running automations.

Conversation-first layout with slash discovery, management views, modal overlays, and animated activity.
Messages typed while the agent is busy are queued. Run `/help` for commands and navigation.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from datetime import datetime

from rich.syntax import Syntax
from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.command import Hit, Hits, Provider
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.markup import escape
from textual.widgets import Button, DataTable, OptionList, RichLog, Static, TabbedContent, TabPane

from forge.client import ClientError, ForgeClient
from forge.tui import screens
from forge.tui.clipboard import local_windows, set_windows_clipboard
from forge.tui.commands import BY_NAME, COMMANDS
from forge.tui.widgets import ActivityIndicator, Composer, StatusBar, Transcript
from forge.tui.style import CSS, FORGE_BLACK


def ts(t: float | None) -> str:
    return datetime.fromtimestamp(t).strftime("%m-%d %H:%M:%S") if t else ""


def _esc(s: str) -> str:
    return str(s).replace("[", r"\[")


def _status(s: str) -> str:
    return {"succeeded": "✓ succeeded", "failed": "✗ failed", "running": "▶ running", "queued": "… queued",
            "waiting_approval": "⚠ approval", "waiting_user": "⚠ needs you", "cancelled": "■ cancelled"}.get(s, s)


def _k(n: int | None) -> str:
    return "?" if n is None else f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def _until(expires: str | None) -> str:
    """Ollama's expires_at (RFC 3339, nanoseconds) as 'unloads in 27 min'."""
    if not expires:
        return ""
    try:
        t = datetime.fromisoformat(re.sub(r"(\.\d{6})\d+", r"\1", expires.replace("Z", "+00:00"))).timestamp()
    except ValueError:
        return ""
    left = t - time.time()
    if left > 365 * 86400:
        return "stays loaded"
    return "unloading" if left <= 0 else f"unloads in {int(left // 60)} min" if left >= 60 else f"unloads in {int(left)} s"


def status_markup(st: dict | None, ctx: dict | None, loaded: list[dict], session_title: str = "") -> str:
    """The /status report (Textual markup; the compact link calls ForgeTUI.action_compact)."""
    if st is None:
        return "[red]●[/red] forged is not reachable. /restart to start it again."
    lines = []
    if ctx:
        lines.append(f"[b]Session[/b]  {escape(session_title or 'New conversation')}  [dim]{ctx['session_id']}[/dim]")
        window, used = ctx.get("window"), ctx.get("used")
        measured = used is not None
        used = used if measured else ctx.get("estimate")
        if window and used is not None:
            frac = min(1.0, used / window)
            col = "green" if frac < 0.6 else "yellow" if frac < 0.85 else "red"
            bar = "█" * round(frac * 20) + "░" * (20 - round(frac * 20))
            src = "last call" if measured else "estimate for the next call"
            lines.append(f"[b]Context[/b]  [{col}]{bar}[/{col}]  {'' if measured else '~'}{_k(used)} of {_k(window)} used · "
                         f"[b]{_k(max(0, window - used))} left[/b] ({100 - round(frac * 100)}%)  [dim]{src}, {escape(ctx['model'])}[/dim]")
        else:
            lines.append("[b]Context[/b]  no tool-calling model configured")
        hist = (f"[b]History[/b]  {ctx['history_messages']} messages (~{_k(ctx['history_tokens'])} tokens) · "
                f"last {ctx['sent_messages']} sent each step ({_k(ctx['history_budget'])} budget)")
        if ctx["dropped_messages"]:
            hist += f" · [yellow]{ctx['dropped_messages']} older not sent[/yellow]"
        if ctx["summary"]:
            hist += f" · summary of {ctx['compacted_messages']} compacted messages"
        lines.append(hist)
    else:
        lines.append("[b]Session[/b]  none yet; context appears once you send a message")
    models = [m for m in loaded if "name" in m]
    if models:
        for i, m in enumerate(models):
            label = "[b]Models[/b]   " if i == 0 else "         "
            spill = m["size_mb"] - m["vram_mb"]
            mem = f"{m['vram_mb'] / 1024:.1f} GB VRAM" + (f" + {spill / 1024:.1f} GB RAM" if spill > 100 else "")
            extra = " · ".join(x for x in (f"ctx {m['ctx']}" if m.get("ctx") else "", _until(m.get("expires_at"))) if x)
            lines.append(f"{label}{escape(m['name'])}  {mem}" + (f" · {extra}" if extra else "") + f"  [dim]@{escape(m['node'])}[/dim]")
    else:
        err = next((m["error"] for m in loaded if "error" in m), None)
        lines.append("[b]Models[/b]   " + (f"[red]Ollama unreachable[/red] [dim]{escape(err)}[/dim]" if err
                                          else "none loaded (the next message loads one)"))
    gpu = st.get("gpu") or {}
    if gpu.get("mem_total_mb"):
        lines.append(f"[b]GPU[/b]      {gpu['mem_used_mb'] / 1024:.1f} of {gpu['mem_total_mb'] / 1024:.1f} GB used · {gpu.get('util_pct', 0)}% busy"
                     + (f"  [dim]{escape(gpu['name'])}[/dim]" if gpu.get("name") else ""))
    up = int(st.get("uptime_s") or 0)
    estop = "[red]E-STOP ON[/red]" if st.get("emergency_stop") else "e-stop off"
    lines.append(f"[b]Daemon[/b]   up {up // 3600}h {up % 3600 // 60}m · {len(st.get('active_runs') or [])} runs active · "
                 f"{st.get('pending_approvals', 0)} approvals pending · {estop}"
                 + (f" · [yellow]restart needed: {escape(', '.join(st['restart_required']))}[/yellow]" if st.get("restart_required") else ""))
    if ctx and ctx.get("busy"):
        lines.append("\n[dim]Compacting is available once the current turn finishes.[/dim]")
    elif ctx and ctx.get("can_compact"):
        lines.append("\n[b][@click=app.compact]▸ Compact now[/][/b]  fold older messages into a summary the model keeps  [dim](or /compact)[/dim]")
    return "\n".join(lines)


class SlashProvider(Provider):
    """Ctrl+P command palette over the same command registry as the slash autocomplete."""

    async def search(self, query: str) -> Hits:
        m = self.matcher(query)
        for c in COMMANDS:
            score = m.match(f"/{c.name} {c.description}")
            if score > 0:
                yield Hit(score, m.highlight(f"/{c.name}  {c.description}"),
                          lambda c=c: self.app.run_worker(self.app.slash(f"/{c.name}")),  # type: ignore[attr-defined]
                          help=c.category)


class ForgeTUI(App):
    TITLE = "Forge"
    COMMANDS = App.COMMANDS | {SlashProvider}
    CSS = CSS
    BINDINGS = [
        Binding("escape", "back_to_chat", "Back to chat", show=False),
        Binding("f1", "tab('chat')", "Chat"), Binding("f2", "tab('runs')", "Runs"), Binding("f3", "tab('workflows')", "Workflows"),
        Binding("f4", "tab('approvals')", "Approvals"), Binding("f5", "tab('records')", "Records"), Binding("f6", "tab('memory')", "Memory"),
        Binding("f7", "tab('activity')", "Activity"),
        Binding("ctrl+o", "open('settings')", "Settings"), Binding("ctrl+t", "open('work')", "Work"),
        Binding("ctrl+x", "open('sessions')", "Sessions"), Binding("ctrl+e", "estop", "E-STOP"), Binding("ctrl+q", "quit", "Quit"),
        Binding("a", "approve", "Approve", show=False), Binding("x", "deny", "Deny", show=False), Binding("r", "run_wf", "Run", show=False),
        Binding("b", "rollback", "Rollback", show=False), Binding("e", "export", "Export", show=False), Binding("c", "cancel_run", "Cancel", show=False),
        Binding("d", "disable_wf", "Disable", show=False),
    ]

    def __init__(self, session: str | None = None, continue_last: bool = False):
        super().__init__()
        self.register_theme(FORGE_BLACK)
        self.theme = "forge-black"
        self.c = ForgeClient()
        self.session_id: str | None = session
        self.continue_last = continue_last
        self.session_title = ""
        self.state = "ready"
        self.busy = False
        self.queue: list[str] = []
        self._interrupt_pending = False
        self.last_status: dict | None = None
        self._estop_armed = 0.0
        self._streaming = False
        self._buf = ""
        self._open_approvals: set[str] = set()
        self.tools: list[dict] = []
        self.show_timeline = True
        self.active_tools: dict[str, str] = {}
        self._event_connected = True
        self._turn_message = False
        self.sel = {"approval": None, "workflow": None, "run": None, "collection": None}

    # ------------------------------------------------------------ layout
    def compose(self) -> ComposeResult:
        yield StatusBar("connecting to forged…", id="status")
        with TabbedContent(initial="chat"):
            with TabPane("Chat [F1]", id="chat"):
                with Vertical(id="conversation"):
                    yield Transcript(id="chatlog")
                    with Horizontal(id="scroll-controls"):
                        yield Button("↓ Latest", id="scroll-bottom", tooltip="Scroll to the latest message and follow new text")
                yield ActivityIndicator(id="activity-indicator")
                suggest = OptionList(id="suggest")
                yield suggest
                with Horizontal(id="input-row"):
                    yield Static("›", id="prompt")
                    yield Composer(suggest, id="composer", placeholder="Ask anything, or / for commands",
                                   highlight_cursor_line=False)
            with TabPane("Runs [F2]", id="runs"):
                with Horizontal(classes="management-split"):
                    yield DataTable(id="runs_t", cursor_type="row")
                    yield RichLog(id="run_detail", classes="detail", wrap=True, markup=True)
            with TabPane("Workflows [F3]", id="workflows"):
                with Horizontal(classes="management-split"):
                    yield DataTable(id="wf_t", cursor_type="row")
                    with VerticalScroll(classes="detail"):
                        yield Static(id="wf_detail")
            with TabPane("Approvals [F4]", id="approvals"):
                with Vertical():
                    yield DataTable(id="ap_t", cursor_type="row")
                    with VerticalScroll(classes="detail"):
                        yield Static(id="ap_detail")
            with TabPane("Records [F5]", id="records"):
                with Vertical():
                    yield DataTable(id="col_t", cursor_type="row")
                    yield DataTable(id="rec_t", cursor_type="row")
            with TabPane("Memory [F6]", id="memory"):
                yield DataTable(id="mem_t", cursor_type="row")
            with TabPane("Activity [F7]", id="activity"):
                yield RichLog(id="activity_log", wrap=True, markup=True, max_lines=3000)
        yield Static("/help commands  ·  Ctrl+O settings", id="hints")

    async def on_mount(self) -> None:
        self.set_class(self.size.width < 100, "compact")
        self.query_one("#runs_t", DataTable).add_columns("run", "workflow", "trigger", "status", "created", "error")
        self.query_one("#wf_t", DataTable).add_columns("workflow", "deployed", "latest", "last run")
        self.query_one("#ap_t", DataTable).add_columns("id", "kind", "title", "requested", "expires")
        self.query_one("#col_t", DataTable).add_columns("collection", "title", "records", "conflicts")
        self.query_one("#mem_t", DataTable).add_columns("id", "kind", "provenance", "content")
        self.chat("[dim]Describe a task to begin. Type / to explore commands and tools.[/dim]")
        try:
            settings = {s["key"]: s["value"] for s in (await self.c.get("/settings"))["settings"]}
            try:
                self.tools = await self.c.get("/tools")
            except Exception:
                self.tools = []
            self.apply_tui_settings(settings)
            sessions = await self.c.get("/sessions")
            if self.session_id is None and self.continue_last and sessions:
                self.session_id = sessions[0]["id"]
            if self.session_id:
                await self.load_session(self.session_id)
        except Exception as e:
            self.chat(f"[red]cannot reach forged: {_esc(e)}. Run `forge` to start it.[/red]")
        self.listen()
        self.set_interval(2, self.refresh_status)
        self.set_interval(5, self.refresh_tables)
        await self.refresh_status()
        await self.refresh_tables()
        self.query_one("#composer").focus()

    def on_resize(self) -> None:
        self.set_class(self.size.width < 100, "compact")

    async def load_session(self, sid: str) -> None:
        # Fetch before switching so a missing session or network error leaves the current chat intact.
        from forge.client import read_chat
        s, messages = await read_chat(self.c, sid)
        self.query_one("#composer", Composer).set_prompt_history([m["content"] for m in messages if m["role"] == "user"])
        self.session_id = sid
        self.queue.clear()
        self.query_one("#chatlog", Transcript).clear()
        self.active_tools.clear()
        self._buf = ""
        self._streaming = False
        self._turn_message = False
        self.busy = s.get("status") == "running"
        self.set_state("thinking" if self.busy else "ready")
        self.session_title = (s or {}).get("title") or ""
        for m in messages:
            self.render_message(m)
        self.chat(f"[dim]— session {sid} —[/dim]")
        self.action_tab("chat")
        self.render_status()

    async def ensure_session(self) -> str:
        if not self.session_id:
            self.session_id = (await self.c.post("/sessions"))["session_id"]
        return self.session_id

    # ------------------------------------------------------------ helpers
    def copy_to_clipboard(self, text: str) -> None:
        super().copy_to_clipboard(text)   # OSC 52: works over SSH and in Windows Terminal
        if local_windows():
            try:
                set_windows_clipboard(text)
            except OSError:
                pass

    def chat(self, markup: str) -> None:
        self.query_one("#chatlog", Transcript).write(Text.from_markup(markup))

    def timeline(self, markup: str) -> None:
        if self.show_timeline:
            self.chat(markup)

    def render_message(self, m: dict) -> None:
        if m["role"] == "user":
            self.chat(f"[b]› {_esc(m['content'])}[/b]")
        elif m["role"] == "assistant" and m["content"].strip():
            self.query_one("#chatlog", Transcript).response(m["content"], final=True)
        elif m["role"] == "tool":
            self.timeline(f"[dim]{m['tool_name']}[/dim] {_esc(m['content'][:120])}")
        elif m["role"] == "summary":
            self.chat("[dim]— earlier messages compacted into a summary (/status for context) —[/dim]")

    def set_state(self, state: str) -> None:
        self.state = state
        indicator = self.query_one("#activity-indicator", ActivityIndicator)
        if state.startswith("tool:"):
            label = f"Running {state[5:]}…"
            if len(self.active_tools) > 1:
                label += f" ({len(self.active_tools)} active)"
            indicator.set_state("tool", label)
        else:
            indicator.set_state(state)
        self.render_status()

    def action_tab(self, tab: str) -> None:
        self.query_one(TabbedContent).active = tab
        if tab == "chat":
            self.query_one("#composer").focus()
        else:
            self.query_one({"runs": "#runs_t", "workflows": "#wf_t", "approvals": "#ap_t",
                            "records": "#col_t", "memory": "#mem_t", "activity": "#activity_log"}[tab]).focus()
        self.render_status()

    def action_back_to_chat(self) -> None:
        if len(self.screen_stack) == 1:
            self.action_tab("chat")

    def apply_tui_settings(self, values: dict) -> None:
        theme = values.get("tui.theme", "forge-black")
        self.theme = "forge-black" if theme == "textual-dark" else theme if theme in self.available_themes else "forge-black"
        self.show_timeline = bool(values.get("tui.show_timeline", True))
        self.bell_on_approval = bool(values.get("tui.bell_on_approval", True))
        self.query_one("#activity-indicator", ActivityIndicator).animations = bool(values.get("tui.animations", True))

    def render_status(self) -> None:
        self.query_one("#status", StatusBar).render_status(self.last_status, self.state, self.session_title, len(self.queue))
        indicator = self.query_one("#activity-indicator", ActivityIndicator)
        indicator.offline = self.last_status is None or not self._event_connected
        indicator.estop = bool((self.last_status or {}).get("emergency_stop"))
        indicator.tick()
        tab = self.query_one(TabbedContent).active
        hints = "/help commands  ·  Ctrl+O settings" if tab == "chat" else f"{tab.title()}  ·  Esc back to chat  ·  /help shortcuts"
        if self.queue:
            hints += f"  ·  {len(self.queue)} messages queued"
        active = len((self.last_status or {}).get("active_runs") or [])
        if active:
            hints += f"  ·  {active} background runs"
        self.query_one("#hints", Static).update(hints)

    # ------------------------------------------------------------ overlays
    def action_open(self, name: str) -> None:
        if name == "settings":
            self.push_screen(screens.SettingsScreen(), lambda _r: self.run_worker(self.refresh_status()))
        elif name == "work":
            self.push_screen(screens.WorkScreen())
        elif name in ("sessions", "history", "resume"):
            self.push_screen(screens.SessionsScreen(), self._session_chosen)
        elif name == "models":
            self.push_screen(screens.ModelsScreen())
        elif name == "tools":
            self.push_screen(screens.ToolsScreen())
        elif name == "doctor":
            self.push_screen(screens.DoctorScreen())
        elif name == "help":
            self.push_screen(screens.HelpScreen())

    def _session_chosen(self, sid: str | None) -> None:
        if sid == "__new__":
            self.query_one("#composer", Composer).set_prompt_history([])
            self.queue.clear()
            self.session_id, self.session_title = None, ""
            self.query_one("#chatlog", Transcript).clear()
            self.active_tools.clear()
            self.busy = False
            self._buf = ""
            self._streaming = False
            self._turn_message = False
            self.set_state("ready")
            self.chat("[dim]new session — it starts with your next message[/dim]")
        elif sid:
            self.run_worker(self._resume_session(sid), group="resume-session", exclusive=True)

    async def _resume_session(self, sid: str) -> None:
        try:
            await self.load_session(sid)
        except ClientError as e:
            self.chat(f"[red]Could not resume chat: {_esc(e)}[/red]")

    async def show_approval(self, approval_id: str) -> None:
        if approval_id in self._open_approvals:
            return
        rows = await self.c.get("/approvals")
        ap = next((a for a in rows if a["id"] == approval_id), None)
        if not ap:
            return
        self._open_approvals.add(approval_id)
        if getattr(self, "bell_on_approval", True):
            self.bell()

        def done(result):
            self._open_approvals.discard(approval_id)
            if result is not None:
                self.run_worker(self._decide_id(approval_id, result["approve"], result.get("reason", "")))
        self.push_screen(screens.ApprovalScreen(ap), done)

    async def _decide_id(self, aid: str, approve: bool, reason: str = "") -> None:
        try:
            r = await self.c.post(f"/approvals/{aid}", {"approve": approve, "reason": reason})
            self.notify(f"{r['approval']['status']}: {r['approval']['title']}")
        except ClientError as e:
            self.notify(str(e), severity="error")
        await self.refresh_tables()

    # ------------------------------------------------------------ live events
    @work(exclusive=True, group="ws")
    async def listen(self) -> None:
        import websockets
        while True:
            try:
                async with websockets.connect(self.c.ws_url, max_size=2**22) as ws:
                    self._event_connected = True
                    self.render_status()
                    async for raw in ws:
                        self.handle_forge_event(json.loads(raw))
            except Exception as e:
                self._event_connected = False
                self.render_status()
                self.query_one("#activity_log", RichLog).write(f"[red]event stream lost ({e}); reconnecting…[/red]")
                await asyncio.sleep(2)

    def handle_forge_event(self, ev: dict) -> None:
        k = ev["kind"]
        if k != "agent.delta":
            detail = {x: y for x, y in ev.items() if x not in ("seq", "ts", "kind")}
            self.query_one("#activity_log", RichLog).write(
                Text.from_markup(f"[dim]{ts(ev['ts'])}[/dim] [b]{k}[/b] {_esc(json.dumps(detail, default=str)[:220])}"))
        top = self.screen
        if hasattr(top, "on_forge_event"):
            top.on_forge_event(ev)
        mine = self.session_id is not None and ev.get("session_id") == self.session_id
        transcript = self.query_one("#chatlog", Transcript)
        if mine and k == "agent.delta":
            if not self._streaming:
                self._streaming, self._buf = True, ""
            self._buf += ev["text"]
            self.set_state("responding")
            transcript.response(self._buf)
        elif mine and k == "agent.thinking":
            if not self.busy:
                self.query_one("#activity-indicator", ActivityIndicator).begin()
                self._turn_message = False
            self.busy = True
            self.set_state("thinking")
        elif mine and k == "agent.message":
            self._streaming = False
            transcript.response(ev["text"], final=True)
            self._buf = ""
            self._turn_message = True
        elif mine and k == "tool.start":
            if self._streaming:
                transcript.response(self._buf, final=True)
                self._streaming, self._buf = False, ""
            self.active_tools[ev.get("call_id", ev["tool"])] = ev["tool"]
            self.set_state(f"tool:{ev['tool']}")
        elif mine and k in ("tool.end", "tool.error"):
            self.active_tools.pop(ev.get("call_id", ev["tool"]), None)
            if k == "tool.end":
                self.timeline(f"[green]●[/green] {_esc(ev['tool'])}  [dim]completed · {ev.get('ms', 0)}ms[/dim]")
            else:
                self.timeline(f"[red]●[/red] {_esc(ev['tool'])}  {_esc(ev.get('error', '')[:160])}")
            self.set_state(f"tool:{next(iter(self.active_tools.values()))}" if self.active_tools else "thinking")
        elif mine and k == "agent.waiting_approval":
            self.set_state("approval")
        elif mine and k == "agent.done":
            final = ev.get("text", "")
            if final and not self._turn_message:
                transcript.response(final, final=True)
            self._streaming, self._buf = False, ""
            self.active_tools.clear()
            self.busy = False
            self.set_state("interrupted" if final == "(interrupted)" else "error" if final.startswith("I hit an error") else "ready")
            if self.queue and final != "(interrupted)" and not self._interrupt_pending and not (self.last_status or {}).get("emergency_stop"):
                nxt = self.queue.pop(0)
                self.run_worker(self._send_chat(nxt))
        elif k == "approval.requested":
            self.run_worker(self.show_approval(ev["approval_id"]))
        elif k == "notify":
            self.notify(f"{ev.get('title')}: {ev.get('body', '')}", timeout=8)
        elif k in ("run.failed", "run.waiting_user"):
            self.notify(f"{k}: {ev.get('run_id')} {ev.get('error') or ev.get('reason') or ''}"[:200], severity="error", timeout=10)
        elif k == "config.changed":
            self.run_worker(self.refresh_tui_settings())
        elif mine and k == "desktop.step":
            colour = "green" if ev.get("verified") else "yellow"
            self.timeline(f"[{colour}]●[/{colour}] {_esc(ev.get('step_text', ''))}")

    async def refresh_tui_settings(self) -> None:
        try:
            values = {s["key"]: s["value"] for s in (await self.c.get("/settings"))["settings"]}
            self.apply_tui_settings(values)
            self.tools = await self.c.get("/tools")
            self.query_one("#composer", Composer)._refresh_suggest()
        except ClientError:
            pass
        await self.refresh_status()

    # ------------------------------------------------------------ status + tables
    async def refresh_status(self) -> None:
        try:
            self.last_status = await self.c.get("/status")
        except Exception:
            self.last_status = None
        self.render_status()

    async def refresh_tables(self) -> None:
        try:
            await self._fill("#runs_t", await self.c.get("/runs", limit=60),
                             lambda r: (r["id"], r["workflow_id"], r["trigger"], _status(r["status"]), ts(r["created_at"]), (r["error"] or "")[:60]), "id")
            await self._fill("#wf_t", await self.c.get("/workflows"),
                             lambda w: (w["id"], f"v{w['deployed']}" if w["deployed"] else "-", f"v{w['latest']}", w["last_run_status"] or ""), "id")
            await self._fill("#ap_t", await self.c.get("/approvals"),
                             lambda a: (a["id"], a["kind"], a["title"][:90], ts(a["requested_at"]), ts(a["expires_at"])), "id")
            await self._fill("#col_t", await self.c.get("/records"), lambda c: (c["name"], c["title"], c["count"], c["conflicts"]), "name")
            if self.query_one(TabbedContent).active == "memory":
                await self._fill("#mem_t", await self.c.get("/memory"), lambda m: (m["id"], m["kind"], m["provenance"], m["content"][:120]), "id")
        except Exception:
            pass

    async def _fill(self, sel: str, rows: list[dict], fmt, key: str) -> None:
        t = self.query_one(sel, DataTable)
        cur = t.cursor_row
        t.clear()
        for r in rows:
            t.add_row(*[str(x) for x in fmt(r)], key=str(r[key]))
        if rows and cur is not None:
            t.move_cursor(row=min(cur, len(rows) - 1))

    @on(DataTable.RowHighlighted)
    async def row_selected(self, ev: DataTable.RowHighlighted) -> None:
        tid = ev.data_table.id
        key = ev.row_key.value if ev.row_key else None
        if tid not in ("runs_t", "wf_t", "ap_t", "col_t") or not key:   # modal tables bubble here too
            return
        try:
            if tid == "runs_t":
                self.sel["run"] = key
                d = await self.c.get(f"/runs/{key}")
                log = self.query_one("#run_detail", RichLog)
                log.clear()
                r = d["run"]
                log.write(Text.from_markup(f"[b]{r['id']}[/b] {r['workflow_id']}  {_status(r['status'])}  attempt {r['attempt']}"))
                if r.get("error"):
                    log.write(Text(f"error ({r['error_class']}): {r['error']}", style="red"))
                for s in d["steps"]:
                    col = {"succeeded": "green", "failed": "red", "running": "yellow", "skipped": "dim"}.get(s["status"], "white")
                    log.write(Text.from_markup(f"[{col}]{s['status']:>16}[/{col}] {s['step_path']}  {_esc((s['error'] or s['output_json'] or '')[:150])}"))
            elif tid == "wf_t":
                self.sel["workflow"] = key
                d = await self.c.get(f"/workflows/{key}")
                vs = "\n".join(f"v{v['version']} {v['status']:9s} by {v['created_by']:8s} {v['note'][:60]}" for v in d["versions"])
                sch = "\n".join(f"next fire: {ts(s['next_fire_at'])}  {s['trigger_json']}" for s in d["schedules"])
                self.query_one("#wf_detail", Static).update(Syntax(d["yaml"] + "\n# versions\n# " + vs.replace("\n", "\n# ") +
                                                                   ("\n# " + sch.replace("\n", "\n# ") if sch else ""), "yaml", theme="ansi_dark"))
            elif tid == "ap_t":
                self.sel["approval"] = key
                a = next((x for x in await self.c.get("/approvals") if x["id"] == key), None)
                if a:
                    self.query_one("#ap_detail", Static).update(Syntax(json.dumps(json.loads(a["subject_json"]), indent=2, default=str)[:8000],
                                                                       "json", theme="ansi_dark"))
            elif tid == "col_t":
                self.sel["collection"] = key
                d = await self.c.get(f"/records/{key}", limit=300)
                t = self.query_one("#rec_t", DataTable)
                t.clear(columns=True)
                cols = ["id"] + d["fields"][:9]
                t.add_columns(*cols)
                for r in d["records"]:
                    t.add_row(*[str(r.get(c, "") or "")[:40] for c in cols])
        except ClientError as e:
            self.notify(str(e), severity="error")

    # ------------------------------------------------------------ input
    @on(Transcript.PositionChanged)
    def _transcript_position(self, event: Transcript.PositionChanged) -> None:
        self.query_one("#scroll-controls").display = event.away_from_bottom

    @on(Button.Pressed, "#scroll-bottom")
    def _scroll_to_latest(self, event: Button.Pressed) -> None:
        event.stop()
        self.query_one("#chatlog", Transcript).anchor()
        self.query_one("#composer", Composer).focus()

    @on(Composer.Submitted)
    def _submitted(self, ev: Composer.Submitted) -> None:
        text = ev.text.strip()
        self.query_one("#composer", Composer).remember_prompt(text)
        if text.startswith("/"):
            self.run_worker(self.slash(text))
        elif self.busy:
            self.queue.append(text)
            self.chat(f"[dim cyan]\\[queued][/dim cyan] {_esc(text)}")
            self.render_status()
        else:
            self.run_worker(self._send_chat(text))

    @on(Composer.Interrupt)
    async def _interrupt(self, _ev) -> None:
        await self.action_interrupt()

    @on(Composer.QuitRequested)
    def _quit(self, _ev) -> None:
        self.exit()

    @on(Composer.ToolSelected)
    def _tool_selected(self, ev: Composer.ToolSelected) -> None:
        state = "enabled" if ev.tool.enabled else "disabled"
        colour = "green" if ev.tool.enabled else "yellow"
        self.chat(f"[{colour}]●[/{colour}] [b]{_esc(ev.tool.name)}[/b] [dim]({state})[/dim]\n{_esc(ev.tool.description)}")

    @on(OptionList.OptionSelected, "#suggest")
    def _suggestion_selected(self, ev: OptionList.OptionSelected) -> None:
        composer = self.query_one("#composer", Composer)
        composer.suggest.highlighted = ev.option_index
        composer._accept()
        composer.focus()

    async def _send_chat(self, text: str) -> None:
        sid = await self.ensure_session()
        self.query_one("#chatlog", Transcript).anchor()
        self.chat(f"[b]› {_esc(text)}[/b]")
        self.query_one("#activity-indicator", ActivityIndicator).begin()
        self._turn_message = False
        self.busy = True
        self.set_state("thinking")
        try:
            await self.c.post(f"/sessions/{sid}/chat", {"text": text})
            if not self.session_title:
                self.session_title = text[:60]
            self.render_status()
        except ClientError as e:
            self.busy = False
            self.set_state("error")
            self.chat(f"[red]●[/red] {_esc(e)}")

    async def slash(self, text: str) -> None:
        cmd, _, rest = text[1:].partition(" ")
        cmd, rest = cmd.lower(), rest.strip()
        if cmd not in BY_NAME:
            self.chat(f"[red]unknown command /{_esc(cmd)}[/red] — /help")
            return
        indicator = self.query_one("#activity-indicator", ActivityIndicator)
        operation = cmd in ("compile", "restart", "export", "compact") and not self.busy
        if operation:
            indicator.begin()
            indicator.set_state("operation", {"compile": "Compiling workflow…", "restart": "Restarting Forge…", "export": "Exporting records…",
                                              "compact": "Compacting context…"}[cmd])
        try:
            await self._slash(cmd, rest)
        except (ClientError, json.JSONDecodeError, ValueError) as e:
            self.chat(f"[red]●[/red] {_esc(str(e))}")
            if operation:
                indicator.set_state("error")
        finally:
            if operation and indicator.state == "operation":
                indicator.set_state("ready")

    async def _slash(self, cmd: str, rest: str) -> None:
        if cmd in ("chat", "runs", "workflows", "approvals", "records", "memory", "activity"):
            self.action_tab(cmd)
        elif cmd in ("settings", "work", "sessions", "tools", "doctor", "help"):
            self.action_open(cmd)
        elif cmd in ("history", "resume"):
            if cmd == "resume" and rest:
                rows = await self.c.get("/sessions", q=rest, limit=2)
                exact = next((s for s in rows if s["id"] == rest), None)
                if exact or len(rows) == 1:
                    await self.load_session((exact or rows[0])["id"])
                    return
            self.push_screen(screens.SessionsScreen(search=rest), self._session_chosen)
        elif cmd == "model":
            self.action_open("models")
        elif cmd == "new":
            self._session_chosen("__new__")
        elif cmd == "title":
            sid = await self.ensure_session()
            await self.c.post(f"/sessions/{sid}/title", {"title": rest})
            self.session_title = rest
            self.set_state(self.state)
        elif cmd == "interrupt":
            await self.action_interrupt()
        elif cmd == "queue":
            if rest == "clear":
                self.queue.clear()
            self.chat("queued: " + (" | ".join(_esc(q) for q in self.queue) or "(empty)"))
            self.set_state(self.state)
        elif cmd == "pin":
            cap, _, model = rest.partition(" ")
            pins = await self.c.post("/models/pin", {"capability": cap, "model": model or None})
            self.chat(f"pins: {_esc(json.dumps(pins))}")
        elif cmd == "set":
            key, _, value = rest.partition(" ")
            r = await self.c.post("/settings", {"key": key, "value": value})
            self.chat(f"[green]✓[/green] {key} = {_esc(json.dumps(r['new'], default=str))} "
                      f"[dim]({'applied' if r['applied'] == 'live' else 'restart required: /restart'})[/dim]")
        elif cmd == "status":
            if rest == "raw":
                self.chat(_esc(json.dumps(await self.c.get("/status"), indent=1, default=str)[:3000]))
                return
            st = await self.c.get("/status")
            ctx = await self.c.get(f"/sessions/{self.session_id}/context") if self.session_id else None
            loaded = ctx.pop("loaded_models") if ctx else await self.c.get("/models/loaded")
            self.query_one("#chatlog", Transcript).write(status_markup(st, ctx, loaded, self.session_title))
        elif cmd == "copy":
            replies = self.query_one("#chatlog", Transcript).responses
            n = int(rest) if rest.isdigit() and int(rest) > 0 else 1
            if len(replies) < n:
                self.chat("[dim]no Forge reply to copy yet[/dim]")
                return
            self.copy_to_clipboard(replies[-n])
            self.notify(f"Copied Forge's {'last' if n == 1 else f'#{n} from last'} reply ({len(replies[-n])} characters)", timeout=3)
        elif cmd == "compact":
            if not self.session_id:
                self.chat("[dim]nothing to compact: no session yet[/dim]")
                return
            if self.busy:
                self.chat("[yellow]the agent is working; compact after this turn finishes (or /interrupt)[/yellow]")
                return
            r = await self.c.post(f"/sessions/{self.session_id}/compact")
            if not r["compacted"]:
                self.chat(f"[dim]{_esc(r.get('reason', 'nothing to compact'))}[/dim]")
                return
            self.chat(f"[green]✓[/green] compacted {r['compacted']} messages: history ~{_k(r['history_tokens_before'])} → "
                      f"~{_k(r['history_tokens_after'])} tokens. The model now sees this summary plus the latest messages:")
            self.chat(f"[dim]{_esc(r['summary'])}[/dim]")
        elif cmd == "reload":
            await self.c.post("/settings/reload")
            self.chat("[dim]settings reloaded from forge.toml[/dim]")
        elif cmd == "restart":
            self.chat("[yellow]restarting forged…[/yellow]")
            from forge import launcher
            await asyncio.to_thread(launcher.stop_daemon)
            await asyncio.to_thread(launcher.ensure_daemon)
            self.chat("[green]forged restarted[/green]")
        elif cmd == "theme":
            names = sorted(self.available_themes)
            nxt = rest if rest in names else names[(names.index(self.theme) + 1) % len(names)] if self.theme in names else names[0]
            self.theme = nxt
            await self.c.post("/settings", {"key": "tui.theme", "value": nxt})
            self.notify(f"theme: {nxt}")
        elif cmd == "compile":
            self.chat("[dim]compiling… (LLM → IR → static checks → tests → dry-run)[/dim]")
            res = await self.c.post("/workflows/compile", {"request": rest})
            if res.get("ok"):
                self.chat(f"[green]compiled {res['workflow_id']} v{res['version']}[/green]; scenarios "
                          f"{sum(t['status'] == 'succeeded' for t in res['tests'])}/{len(res['tests'])} ok; dry-run {res['dry_run']['status']}. "
                          f"Deploy approval [b]{res['approval_id']}[/b] is waiting for you.")
            else:
                self.chat(f"[red]compile failed after {res['attempts']} attempts[/red]: " +
                          _esc("; ".join(i["message"] for i in res["issues"][:6]) or json.dumps(res.get("missing_tools") or res.get("tests"))[:400]))
        elif cmd == "run":
            wid, _, inp = rest.partition(" ")
            r = await self.c.post(f"/workflows/{wid}/run", {"inputs": json.loads(inp) if inp else {}})
            self.chat(f"queued [b]{r['run_id']}[/b]")
        elif cmd == "disable":
            r = await self.c.post(f"/workflows/{rest}/disable")
            self.chat(f"disabled {r['workflow_id']} (v{r['disabled_version']} retired, schedules removed)")
        elif cmd == "enable":
            r = await self.c.post(f"/workflows/{rest}/enable")
            self.chat(f"re-deploy requested: approval [b]{r['approval_id']}[/b]")
        elif cmd in ("approve", "deny"):
            aid, _, reason = rest.partition(" ")
            await self._decide_id(aid, cmd == "approve", reason)
        elif cmd == "remember":
            await self.c.post("/memory", {"content": rest, "kind": "fact"})
            self.chat("[dim]stored as a user-stated fact[/dim]")
        elif cmd == "export":
            col, _, path = rest.partition(" ")
            r = await self.c.post("/records/export", {"collections": [col], "path": path})
            self.chat(f"exported {r['sheets']} → {r['path']} (verified={r['verified']})")
        elif cmd == "stop":
            await self.c.post("/stop")
            await self.refresh_status()
            self.notify("EMERGENCY STOP engaged. /unstop to clear.", severity="error", timeout=10)
        elif cmd == "unstop":
            await self.c.post("/resume")
            await self.refresh_status()
        elif cmd == "quit":
            self.exit()

    async def action_compact(self) -> None:
        await self.slash("/compact")

    async def action_interrupt(self) -> None:
        if self.session_id and self.busy:
            self.queue.clear()
            self._interrupt_pending = True
            self.set_state("interrupting")
            try:
                await self.c.post(f"/sessions/{self.session_id}/interrupt")
            except ClientError as e:
                self.notify(f"Interrupt failed: {e}", severity="error")
            finally:
                self._interrupt_pending = False

    async def action_estop(self) -> None:
        if time.time() - self._estop_armed < 3:
            await self.c.post("/stop")
            await self.refresh_status()
            self.notify("EMERGENCY STOP engaged. /unstop to clear.", severity="error", timeout=10)
        else:
            self._estop_armed = time.time()
            self.notify("Press Ctrl+E again within 3s to EMERGENCY STOP", severity="warning")

    # ------------------------------------------------------------ tab actions
    async def _decide(self, approve: bool) -> None:
        if self.query_one(TabbedContent).active == "approvals" and self.sel["approval"]:
            await self._decide_id(self.sel["approval"], approve)

    async def action_approve(self) -> None:
        await self._decide(True)

    async def action_deny(self) -> None:
        await self._decide(False)

    async def action_run_wf(self) -> None:
        if self.query_one(TabbedContent).active == "workflows" and self.sel["workflow"]:
            r = await self.c.post(f"/workflows/{self.sel['workflow']}/run", {"inputs": {}})
            self.notify(f"queued {r['run_id']}")

    async def action_disable_wf(self) -> None:
        if self.query_one(TabbedContent).active == "workflows" and self.sel["workflow"]:
            try:
                r = await self.c.post(f"/workflows/{self.sel['workflow']}/disable")
                self.notify(f"disabled {r['workflow_id']}")
            except ClientError as e:
                self.notify(str(e), severity="error")
            await self.refresh_tables()

    async def action_rollback(self) -> None:
        if self.query_one(TabbedContent).active == "workflows" and self.sel["workflow"]:
            try:
                self.notify(str(await self.c.post(f"/workflows/{self.sel['workflow']}/rollback")))
            except ClientError as e:
                self.notify(str(e), severity="error")

    async def action_cancel_run(self) -> None:
        if self.query_one(TabbedContent).active == "runs" and self.sel["run"]:
            await self.c.post(f"/runs/{self.sel['run']}/cancel")

    async def action_export(self) -> None:
        if self.query_one(TabbedContent).active == "records" and self.sel["collection"]:
            from forge.config import forge_home
            path = str(forge_home() / "workspace" / f"{self.sel['collection']}.xlsx")
            try:
                r = await self.c.post("/records/export", {"collections": [self.sel["collection"]], "path": path})
                self.notify(f"exported → {r['path']}")
            except ClientError as e:
                self.notify(str(e), severity="error")


def run(session: str | None = None, continue_last: bool = False) -> None:
    ForgeTUI(session=session, continue_last=continue_last).run()
