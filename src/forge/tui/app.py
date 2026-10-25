"""Forge terminal UI (Textual) — a pure client of forged (HTTP commands + WebSocket event stream).
Works over SSH; quitting never affects running automations.

Hermes-style: floating slash autocomplete, modal overlays (settings, models, tools, sessions, approvals, doctor,
work monitor, help), a status bar with state/model/context/GPU/Laya/runs/approvals, and messages typed while the
agent is busy are queued. Run `/help` for everything.
"""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime

from rich.syntax import Syntax
from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.command import Hit, Hits, Provider
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import DataTable, Footer, OptionList, RichLog, Static, TabbedContent, TabPane

from forge.client import ClientError, ForgeClient
from forge.tui import screens
from forge.tui.commands import BY_NAME, COMMANDS
from forge.tui.widgets import Composer, StatusBar


def ts(t: float | None) -> str:
    return datetime.fromtimestamp(t).strftime("%m-%d %H:%M:%S") if t else ""


def _esc(s: str) -> str:
    return str(s).replace("[", r"\[")


def _status(s: str) -> str:
    return {"succeeded": "✓ succeeded", "failed": "✗ failed", "running": "▶ running", "queued": "… queued",
            "waiting_approval": "⚠ approval", "waiting_user": "⚠ needs you", "cancelled": "■ cancelled"}.get(s, s)


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
    CSS = """
    #status { height: 1; background: $boost; padding: 0 1; }
    #chatlog { height: 1fr; border: round $primary; }
    #composer { height: 5; border: round $accent; }
    #suggest { height: auto; max-height: 10; border: round $secondary; display: none; }
    #stream { height: auto; max-height: 6; padding: 0 1; }
    #timeline { width: 48; border: round $secondary; }
    .detail { border: round $secondary; height: 1fr; }
    DataTable { height: 1fr; }
    """
    BINDINGS = [
        Binding("f1", "tab('chat')", "Chat"), Binding("f2", "tab('runs')", "Runs"), Binding("f3", "tab('workflows')", "Workflows"),
        Binding("f4", "tab('approvals')", "Approvals"), Binding("f5", "tab('records')", "Records"), Binding("f6", "tab('memory')", "Memory"),
        Binding("f7", "tab('activity')", "Activity"),
        Binding("ctrl+o", "open('settings')", "Settings"), Binding("ctrl+t", "open('work')", "Work"),
        Binding("ctrl+x", "open('sessions')", "Sessions"), Binding("ctrl+e", "estop", "E-STOP"), Binding("ctrl+q", "quit", "Quit"),
        Binding("a", "approve", "Approve", show=False), Binding("x", "deny", "Deny", show=False), Binding("r", "run_wf", "Run", show=False),
        Binding("b", "rollback", "Rollback", show=False), Binding("e", "export", "Export", show=False), Binding("c", "cancel_run", "Cancel", show=False),
    ]

    def __init__(self, session: str | None = None, continue_last: bool = False):
        super().__init__()
        self.c = ForgeClient()
        self.session_id: str | None = session
        self.continue_last = continue_last
        self.session_title = ""
        self.state = "ready"
        self.busy = False
        self.queue: list[str] = []
        self.last_status: dict | None = None
        self._estop_armed = 0.0
        self._streaming = False
        self._buf = ""
        self._open_approvals: set[str] = set()
        self.sel = {"approval": None, "workflow": None, "run": None, "collection": None}

    # ------------------------------------------------------------ layout
    def compose(self) -> ComposeResult:
        yield StatusBar("connecting to forged…", id="status")
        with TabbedContent(initial="chat"):
            with TabPane("Chat [F1]", id="chat"):
                with Horizontal():
                    with Vertical():
                        yield RichLog(id="chatlog", wrap=True, markup=True, highlight=False)
                        yield Static("", id="stream")
                        suggest = OptionList(id="suggest")
                        yield suggest
                        yield Composer(suggest, id="composer")
                    yield RichLog(id="timeline", wrap=True, markup=True)
            with TabPane("Runs [F2]", id="runs"):
                with Horizontal():
                    yield DataTable(id="runs_t", cursor_type="row")
                    yield RichLog(id="run_detail", classes="detail", wrap=True, markup=True)
            with TabPane("Workflows [F3]", id="workflows"):
                with Horizontal():
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
        yield Footer()

    async def on_mount(self) -> None:
        self.query_one("#runs_t", DataTable).add_columns("run", "workflow", "trigger", "status", "created", "error")
        self.query_one("#wf_t", DataTable).add_columns("workflow", "deployed", "latest", "last run")
        self.query_one("#ap_t", DataTable).add_columns("id", "kind", "title", "requested", "expires")
        self.query_one("#col_t", DataTable).add_columns("collection", "title", "records", "conflicts")
        self.query_one("#mem_t", DataTable).add_columns("id", "kind", "provenance", "content")
        self.chat("[b]Forge[/b] [dim]— type a request, or / for commands (Tab completes). Ctrl+O settings · Ctrl+T work · /help[/dim]")
        try:
            settings = {s["key"]: s["value"] for s in (await self.c.get("/settings"))["settings"]}
            if settings.get("tui.theme") in self.available_themes:
                self.theme = settings["tui.theme"]
            self.query_one("#timeline").display = bool(settings.get("tui.show_timeline", True))
            self.bell_on_approval = bool(settings.get("tui.bell_on_approval", True))
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

    async def load_session(self, sid: str) -> None:
        self.session_id = sid
        self.query_one("#chatlog", RichLog).clear()
        self.query_one("#timeline", RichLog).clear()
        s = next((x for x in await self.c.get("/sessions") if x["id"] == sid), None)
        self.session_title = (s or {}).get("title") or ""
        for m in await self.c.get(f"/sessions/{sid}/messages", limit=60):
            self.render_message(m)
        self.chat(f"[dim]— session {sid} —[/dim]")

    async def ensure_session(self) -> str:
        if not self.session_id:
            self.session_id = (await self.c.post("/sessions"))["session_id"]
        return self.session_id

    # ------------------------------------------------------------ helpers
    def chat(self, markup: str) -> None:
        self.query_one("#chatlog", RichLog).write(Text.from_markup(markup))

    def timeline(self, markup: str) -> None:
        self.query_one("#timeline", RichLog).write(Text.from_markup(markup))

    def render_message(self, m: dict) -> None:
        if m["role"] == "user":
            self.chat(f"[b cyan]you[/b cyan]  {_esc(m['content'])}")
        elif m["role"] == "assistant" and m["content"].strip():
            self.chat(f"[b green]forge[/b green] {_esc(m['content'])}")
        elif m["role"] == "tool":
            self.timeline(f"[dim]{m['tool_name']}[/dim] {_esc(m['content'][:120])}")

    def set_state(self, state: str) -> None:
        self.state = state
        self.query_one("#status", StatusBar).render_status(self.last_status, state, self.session_title, len(self.queue))

    def action_tab(self, tab: str) -> None:
        self.query_one(TabbedContent).active = tab

    # ------------------------------------------------------------ overlays
    def action_open(self, name: str) -> None:
        if name == "settings":
            self.push_screen(screens.SettingsScreen(), lambda _r: self.run_worker(self.refresh_status()))
        elif name == "work":
            self.push_screen(screens.WorkScreen())
        elif name == "sessions":
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
            self.session_id, self.session_title = None, ""
            self.query_one("#chatlog", RichLog).clear()
            self.chat("[dim]new session — it starts with your next message[/dim]")
        elif sid:
            self.run_worker(self.load_session(sid))

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
                    async for raw in ws:
                        self.handle_forge_event(json.loads(raw))
            except Exception as e:
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
        stream = self.query_one("#stream", Static)
        if mine and k == "agent.delta":
            if not self._streaming:
                self._streaming, self._buf = True, ""
            self._buf += ev["text"]
            stream.update(Text("forge ▸ " + self._buf[-600:], style="green"))
        elif mine and k == "agent.thinking":
            self.busy = True
            self.set_state("thinking")
            stream.update(Text(f"forge is thinking… (step {ev.get('step')})", style="dim"))
        elif mine and k == "agent.message":
            self._streaming = False
            stream.update("")
            self.chat(f"[b green]forge[/b green] {_esc(ev['text'])}")
        elif mine and k == "tool.start":
            self._streaming = False
            self.set_state(f"tool:{ev['tool']}")
            self.timeline(f"[cyan]→ {ev['tool']}[/cyan] {_esc(ev.get('args', '')[:150])}")
        elif mine and k == "tool.end":
            self.timeline(f"[green]  ✓ {ev['tool']}[/green] {ev.get('ms', '')}ms")
        elif mine and k == "tool.error":
            self.timeline(f"[red]  ✗ {ev['tool']}: {_esc(ev.get('error', '')[:160])}[/red]")
        elif mine and k == "agent.waiting_approval":
            self.set_state("approval")
        elif mine and k == "agent.done":
            stream.update("")
            self.busy = False
            self.set_state("interrupted" if ev.get("text") == "(interrupted)" else "ready")
            self.timeline(f"[dim]done · {ev.get('steps')} steps · {ev.get('tokens')} tok[/dim]")
            if self.queue:
                nxt = self.queue.pop(0)
                self.run_worker(self._send_chat(nxt))
        elif k == "approval.requested":
            self.run_worker(self.show_approval(ev["approval_id"]))
        elif k == "notify":
            self.notify(f"{ev.get('title')}: {ev.get('body', '')}", timeout=8)
        elif k in ("run.failed", "run.waiting_user"):
            self.notify(f"{k}: {ev.get('run_id')} {ev.get('error') or ev.get('reason') or ''}"[:200], severity="error", timeout=10)
        elif k == "config.changed":
            self.run_worker(self.refresh_status())
        elif k == "desktop.step":
            self.timeline(f"[magenta]🖥 {_esc(ev.get('step_text', ''))}[/magenta] {'✓' if ev.get('verified') else '✗'} S{ev.get('systems')}")

    # ------------------------------------------------------------ status + tables
    async def refresh_status(self) -> None:
        try:
            self.last_status = await self.c.get("/status")
        except Exception:
            self.last_status = None
        self.query_one("#status", StatusBar).render_status(self.last_status, self.state, self.session_title, len(self.queue))

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
    @on(Composer.Submitted)
    def _submitted(self, ev: Composer.Submitted) -> None:
        text = ev.text.strip()
        if text.startswith("/"):
            self.run_worker(self.slash(text))
        elif self.busy:
            self.queue.append(text)
            self.chat(f"[dim cyan]\\[queued][/dim cyan] {_esc(text)}")
            self.set_state("queued")
        else:
            self.run_worker(self._send_chat(text))

    @on(Composer.Interrupt)
    async def _interrupt(self, _ev) -> None:
        await self.action_interrupt()

    @on(Composer.QuitRequested)
    def _quit(self, _ev) -> None:
        self.exit()

    async def _send_chat(self, text: str) -> None:
        sid = await self.ensure_session()
        self.chat(f"[b cyan]you[/b cyan]  {_esc(text)}")
        try:
            await self.c.post(f"/sessions/{sid}/chat", {"text": text})
            self.busy = True
            self.set_state("thinking")
            if not self.session_title:
                self.session_title = text[:60]
        except ClientError as e:
            self.chat(f"[red]{_esc(e)}[/red]")

    async def slash(self, text: str) -> None:
        cmd, _, rest = text[1:].partition(" ")
        cmd, rest = cmd.lower(), rest.strip()
        if cmd not in BY_NAME:
            self.chat(f"[red]unknown command /{_esc(cmd)}[/red] — /help")
            return
        try:
            await self._slash(cmd, rest)
        except (ClientError, json.JSONDecodeError, ValueError) as e:
            self.chat(f"[red]{_esc(str(e))}[/red]")

    async def _slash(self, cmd: str, rest: str) -> None:
        if cmd in ("settings", "work", "sessions", "tools", "doctor", "help"):
            self.action_open(cmd)
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
            self.chat(_esc(json.dumps(await self.c.get("/status"), indent=1, default=str)[:3000]))
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
        elif cmd == "workflows":
            for w in await self.c.get("/workflows"):
                self.chat(f"{w['id']}  deployed v{w['deployed']}  latest v{w['latest']}  last: {w['last_run_status']}")
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
            self.notify("EMERGENCY STOP engaged. /resume to clear.", severity="error", timeout=10)
        elif cmd == "resume":
            await self.c.post("/resume")
        elif cmd == "quit":
            self.exit()

    async def action_interrupt(self) -> None:
        if self.session_id and self.busy:
            await self.c.post(f"/sessions/{self.session_id}/interrupt")
            self.queue.clear()

    async def action_estop(self) -> None:
        if time.time() - self._estop_armed < 3:
            await self.c.post("/stop")
            self.notify("EMERGENCY STOP engaged. /resume to clear.", severity="error", timeout=10)
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
