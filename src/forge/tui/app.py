"""Forge terminal UI (Textual). A pure client of forged: HTTP for commands, WebSocket for the live event stream.
Works over SSH (e.g. Ghostty on the Mac -> OpenSSH on the Windows host -> `forge tui`). Quitting never affects runs.

Keys: F1 Chat  F2 Runs  F3 Workflows  F4 Approvals  F5 Records  F6 Memory  F7 Activity
      Enter send   Alt+Enter / Ctrl+N newline   Ctrl+I interrupt agent   Ctrl+X ×2 EMERGENCY STOP   Ctrl+Q quit
      Approvals: a approve, x deny     Workflows: r run, b rollback     Records: e export to Excel     Runs: c cancel
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
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import DataTable, Footer, Label, RichLog, Static, TabbedContent, TabPane, TextArea

from forge.client import ClientError, ForgeClient

HELP = """[b]Slash commands[/b]
/new                     new chat session          /sessions            list sessions
/compile <request>       build a workflow          /workflows           list workflows
/run <workflow> [json]   run a workflow            /status              daemon status
/approve <id> [reason]   approve                   /deny <id> [reason]  deny
/remember <fact>         store a user-stated fact (trusted)
/model <capability> [model]  pin/unpin a model     /export <collection> <path.xlsx>
/stop  /resume           emergency stop / resume   /help"""


def ts(t: float | None) -> str:
    return datetime.fromtimestamp(t).strftime("%m-%d %H:%M:%S") if t else ""


class ChatInput(TextArea):
    BINDINGS = [Binding("enter", "send", "send", show=False), Binding("alt+enter,ctrl+n", "newline", "newline", show=False)]

    def action_send(self) -> None:
        self.app.send_chat(self.text)  # type: ignore[attr-defined]
        self.clear()

    def action_newline(self) -> None:
        self.insert("\n")

    async def _on_key(self, event) -> None:
        if event.key == "enter":
            event.prevent_default()
            event.stop()
            self.action_send()
            return
        await super()._on_key(event)


class ForgeTUI(App):
    CSS = """
    #status { height: 1; background: $boost; padding: 0 1; }
    #chatlog { height: 1fr; border: round $primary; }
    #chatinput { height: 5; border: round $accent; }
    #stream { height: auto; max-height: 6; padding: 0 1; }
    #timeline { width: 48; border: round $secondary; }
    .detail { border: round $secondary; height: 1fr; }
    DataTable { height: 1fr; }
    #estop { color: white; background: red; text-style: bold; }
    """
    BINDINGS = [
        Binding("f1", "tab('chat')", "Chat"), Binding("f2", "tab('runs')", "Runs"), Binding("f3", "tab('workflows')", "Workflows"),
        Binding("f4", "tab('approvals')", "Approvals"), Binding("f5", "tab('records')", "Records"), Binding("f6", "tab('memory')", "Memory"),
        Binding("f7", "tab('activity')", "Activity"), Binding("ctrl+i", "interrupt", "Interrupt", show=False),
        Binding("ctrl+x", "estop", "E-STOP"), Binding("ctrl+q", "quit", "Quit"),
        Binding("a", "approve", "Approve", show=False), Binding("x", "deny", "Deny", show=False), Binding("r", "run_wf", "Run", show=False),
        Binding("b", "rollback", "Rollback", show=False), Binding("e", "export", "Export", show=False), Binding("c", "cancel_run", "Cancel", show=False),
    ]

    def __init__(self):
        super().__init__()
        self.c = ForgeClient()
        self.session_id: str | None = None
        self._estop_armed = 0.0
        self._streaming = False
        self.sel = {"approval": None, "workflow": None, "run": None, "collection": None}

    # ------------------------------------------------------------ layout
    def compose(self) -> ComposeResult:
        yield Static("connecting to forged…", id="status")
        with TabbedContent(initial="chat"):
            with TabPane("Chat [F1]", id="chat"):
                with Horizontal():
                    with Vertical():
                        yield RichLog(id="chatlog", wrap=True, markup=True, highlight=False)
                        yield Static("", id="stream")
                        yield ChatInput(id="chatinput")
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
        self.chat("[dim]Forge TUI — type a request, or /help. Enter sends, Alt+Enter newline.[/dim]")
        try:
            sessions = await self.c.get("/sessions")
            self.session_id = sessions[0]["id"] if sessions else (await self.c.post("/sessions"))["session_id"]
            for m in await self.c.get(f"/sessions/{self.session_id}/messages", limit=40):
                self.render_message(m)
        except Exception as e:
            self.chat(f"[red]cannot reach forged: {e}. Start it with `forge daemon`.[/red]")
        self.listen()
        self.set_interval(3, self.refresh_status)
        self.set_interval(5, self.refresh_tables)
        await self.refresh_status()
        await self.refresh_tables()
        self.query_one("#chatinput").focus()

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

    def action_tab(self, tab: str) -> None:
        self.query_one(TabbedContent).active = tab

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
        act = self.query_one("#activity_log", RichLog)
        if k not in ("agent.delta",):
            detail = {x: y for x, y in ev.items() if x not in ("seq", "ts", "kind")}
            act.write(Text.from_markup(f"[dim]{ts(ev['ts'])}[/dim] [b]{k}[/b] {_esc(json.dumps(detail, default=str)[:220])}"))
        mine = ev.get("session_id") == self.session_id
        stream = self.query_one("#stream", Static)
        if mine and k == "agent.delta":
            if not self._streaming:
                self._streaming = True
                self._buf = ""
            self._buf += ev["text"]
            stream.update(Text("forge ▸ " + self._buf[-600:], style="green"))
        elif mine and k == "agent.thinking":
            stream.update(Text(f"forge is thinking… (step {ev.get('step')})", style="dim"))
        elif mine and k == "agent.message":
            self._streaming = False
            stream.update("")
            self.chat(f"[b green]forge[/b green] {_esc(ev['text'])}")
        elif mine and k == "tool.start":
            self._streaming = False
            self.timeline(f"[cyan]→ {ev['tool']}[/cyan] {_esc(ev.get('args', '')[:150])}")
        elif mine and k == "tool.end":
            self.timeline(f"[green]  ✓ {ev['tool']}[/green] {ev.get('ms', '')}ms")
        elif mine and k == "tool.error":
            self.timeline(f"[red]  ✗ {ev['tool']}: {_esc(ev.get('error', '')[:160])}[/red]")
        elif mine and k == "agent.waiting_approval":
            self.chat(f"[b yellow]⚠ approval needed[/b yellow] {_esc(ev.get('reason', ''))} — press F4 (approval {ev['approval_id']})")
            self.bell()
        elif mine and k == "agent.done":
            stream.update("")
            self.timeline(f"[dim]done · {ev.get('steps')} steps · {ev.get('tokens')} tok[/dim]")
        elif k in ("approval.requested",) and not mine:
            self.notify(f"Approval needed: {ev.get('title', '')}", severity="warning", timeout=8)
        elif k == "notify":
            self.notify(f"{ev.get('title')}: {ev.get('body', '')}", timeout=8)
        elif k in ("run.failed", "run.waiting_user"):
            self.notify(f"{k}: {ev.get('run_id')} {ev.get('error') or ev.get('reason') or ''}"[:200], severity="error", timeout=10)
        elif k == "desktop.step":
            self.timeline(f"[magenta]🖥 {_esc(ev.get('step_text', ''))}[/magenta] {'✓' if ev.get('verified') else '✗'} S{ev.get('systems')}")

    # ------------------------------------------------------------ status + tables
    async def refresh_status(self) -> None:
        try:
            s = await self.c.get("/status")
        except Exception:
            self.query_one("#status", Static).update(Text("● forged unreachable", style="bold red"))
            return
        g = s.get("gpu", {})
        nodes = " ".join(f"{'●' if n['healthy'] else '○'}{n['name']}[{','.join(m.split(':')[0] for m in n['loaded']) or '-'}]" for n in s["nodes"])
        gpu = f"GPU {g.get('mem_used_mb', '?')}/{g.get('mem_total_mb', '?')}MB {g.get('util_pct', '?')}%" if g else "GPU ?"
        laya = f"Laya {'●' if s['laya']['loaded'] else '○'} {s['laya']['calls']}"
        runs = s.get("runs", {})
        line = (f"{'⛔ E-STOP  ' if s['emergency_stop'] else ''}{nodes} │ {gpu} │ {laya} │ active {len(s['active_runs'])} · queued {runs.get('queued', 0)}"
                f" · waiting {runs.get('waiting_approval', 0) + runs.get('waiting_user', 0)} │ approvals {s['pending_approvals']} │ tools {s['tools']}")
        self.query_one("#status", Static).update(Text(line, style="bold white on red" if s["emergency_stop"] else ""))

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
        key = ev.row_key.value if ev.row_key else None
        tid = ev.data_table.id
        if not key:
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
                self.query_one("#wf_detail", Static).update(Syntax(d["yaml"] + f"\n# versions\n# " + vs.replace("\n", "\n# ") +
                                                                   ("\n# " + sch.replace("\n", "\n# ") if sch else ""), "yaml", theme="ansi_dark"))
            elif tid == "ap_t":
                self.sel["approval"] = key
                rows = await self.c.get("/approvals")
                a = next((x for x in rows if x["id"] == key), None)
                if a:
                    subj = json.loads(a["subject_json"])
                    self.query_one("#ap_detail", Static).update(Syntax(json.dumps(subj, indent=2, default=str)[:8000], "json", theme="ansi_dark"))
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

    # ------------------------------------------------------------ actions
    def send_chat(self, text: str) -> None:
        text = text.strip()
        if not text:
            return
        self.run_worker(self._send(text), exclusive=False)

    async def _send(self, text: str) -> None:
        if text.startswith("/"):
            await self.slash(text)
            return
        self.chat(f"[b cyan]you[/b cyan]  {_esc(text)}")
        try:
            await self.c.post(f"/sessions/{self.session_id}/chat", {"text": text})
        except ClientError as e:
            self.chat(f"[red]{e}[/red]")

    async def slash(self, text: str) -> None:
        cmd, _, rest = text[1:].partition(" ")
        rest = rest.strip()
        try:
            if cmd == "help":
                self.chat(HELP)
            elif cmd == "new":
                self.session_id = (await self.c.post("/sessions"))["session_id"]
                self.query_one("#chatlog", RichLog).clear()
                self.chat(f"[dim]new session {self.session_id}[/dim]")
            elif cmd == "sessions":
                for s in await self.c.get("/sessions"):
                    self.chat(f"{s['id']}  {s['status']:11s} {s['title']}")
            elif cmd == "compile":
                self.chat(f"[dim]compiling… (LLM → IR → static checks → tests → dry-run)[/dim]")
                res = await self.c.post("/workflows/compile", {"request": rest})
                if res.get("ok"):
                    self.chat(f"[green]compiled {res['workflow_id']} v{res['version']}[/green]; tests: "
                              f"{sum(t['passed'] for t in res['tests'])}/{len(res['tests'])} passed; dry-run {res['dry_run']['status']}. "
                              f"Deploy approval [b]{res['approval_id']}[/b] is in F4.")
                else:
                    self.chat(f"[red]compile failed after {res['attempts']} attempts[/red]: " +
                              _esc("; ".join(i["message"] for i in res["issues"][:6]) or json.dumps(res.get("missing_tools") or res.get("tests"))[:400]))
            elif cmd == "workflows":
                for w in await self.c.get("/workflows"):
                    self.chat(f"{w['id']}  deployed v{w['deployed']}  latest v{w['latest']}  last: {w['last_run_status']}")
            elif cmd == "run":
                wid, _, inp = rest.partition(" ")
                r = await self.c.post(f"/workflows/{wid}/run", {"inputs": json.loads(inp) if inp else {}})
                self.chat(f"queued [b]{r['run_id']}[/b]")
            elif cmd in ("approve", "deny"):
                aid, _, reason = rest.partition(" ")
                r = await self.c.post(f"/approvals/{aid}", {"approve": cmd == "approve", "reason": reason})
                self.chat(f"{r['approval']['status']}: {r['approval']['title']}")
            elif cmd == "remember":
                await self.c.post("/memory", {"content": rest, "kind": "fact"})
                self.chat("[dim]stored as a user-stated fact[/dim]")
            elif cmd == "model":
                cap, _, model = rest.partition(" ")
                self.chat(str(await self.c.post("/models/pin", {"capability": cap, "model": model or None})))
            elif cmd == "export":
                col, _, path = rest.partition(" ")
                r = await self.c.post("/records/export", {"collections": [col], "path": path})
                self.chat(f"exported {r['sheets']} → {r['path']} (verified={r['verified']})")
            elif cmd == "status":
                self.chat(_esc(json.dumps(await self.c.get("/status"), indent=1, default=str)[:3000]))
            elif cmd == "stop":
                await self.c.post("/stop")
            elif cmd == "resume":
                await self.c.post("/resume")
            else:
                self.chat(f"[red]unknown command /{cmd}[/red] — /help")
        except (ClientError, json.JSONDecodeError) as e:
            self.chat(f"[red]{_esc(str(e))}[/red]")

    async def action_interrupt(self) -> None:
        if self.session_id:
            await self.c.post(f"/sessions/{self.session_id}/interrupt")

    async def action_estop(self) -> None:
        if time.time() - self._estop_armed < 3:
            await self.c.post("/stop")
            self.notify("EMERGENCY STOP engaged. /resume to clear.", severity="error", timeout=10)
        else:
            self._estop_armed = time.time()
            self.notify("Press Ctrl+X again within 3s to EMERGENCY STOP", severity="warning")

    async def _decide(self, approve: bool) -> None:
        if self.query_one(TabbedContent).active != "approvals" or not self.sel["approval"]:
            return
        r = await self.c.post(f"/approvals/{self.sel['approval']}", {"approve": approve})
        self.notify(f"{r['approval']['status']}: {r['approval']['title']}")
        await self.refresh_tables()

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


def _esc(s: str) -> str:
    return str(s).replace("[", r"\[")


def _status(s: str) -> str:
    return {"succeeded": "✓ succeeded", "failed": "✗ failed", "running": "▶ running", "queued": "… queued",
            "waiting_approval": "⚠ approval", "waiting_user": "⚠ needs you", "cancelled": "■ cancelled"}.get(s, s)


def run() -> None:
    ForgeTUI().run()
