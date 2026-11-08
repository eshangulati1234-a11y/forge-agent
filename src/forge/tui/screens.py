"""Modal overlays (Hermes-style): settings, model picker, tools, sessions, approval, doctor, work monitor, help.
Every screen talks to forged through the shared ForgeClient (`self.app.c`); nothing here touches state directly."""
from __future__ import annotations

import json
from datetime import datetime

from rich.syntax import Syntax
from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import DataTable, Input, Label, OptionList, Static
from textual.widgets.option_list import Option

from forge.client import ClientError
from forge.tui.commands import COMMANDS, KEYS

MODAL_CSS = """
_Base { align: center middle; }
.modal { width: 92%; height: 88%; border: solid $secondary; background: $background; padding: 0 1; }
.modal-title { text-style: bold; color: $foreground; height: auto; margin-bottom: 1; }
.hint { color: $text-muted; height: auto; }
.err { color: $error; height: auto; }
.ok { color: $success; height: auto; }
DataTable { height: 1fr; }
"""


def ts(t: float | None) -> str:
    return datetime.fromtimestamp(t).strftime("%m-%d %H:%M:%S") if t else ""


def _val(v) -> str:
    return v if isinstance(v, str) else json.dumps(v, default=str)


class _Base(ModalScreen):
    DEFAULT_CSS = MODAL_CSS
    BINDINGS = [Binding("escape", "dismiss_none", "close")]

    def action_dismiss_none(self) -> None:
        self.dismiss(None)

    @property
    def c(self):
        return self.app.c  # type: ignore[attr-defined]

    def flash(self, widget_id: str, msg: str, ok: bool = True) -> None:
        w = self.query_one(widget_id, Static)
        w.set_classes("ok" if ok else "err")
        w.update(msg)


# ------------------------------------------------------------------------------------- settings
class SettingsScreen(_Base):
    BINDINGS = _Base.BINDINGS + [Binding("ctrl+r", "reload", "reload from file")]

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal"):
            yield Label("Settings  ·  Enter edit · Space toggle · Esc close", classes="modal-title")
            with Horizontal():
                yield OptionList(id="sections")
                with Vertical():
                    yield DataTable(id="settings_t", cursor_type="row")
                    yield Static("", id="desc", classes="hint")
                    yield Input(placeholder="new value (Enter to save)", id="value")
                    yield Static("", id="msg")

    async def on_mount(self) -> None:
        self.query_one("#sections", OptionList).styles.width = 18 if self.size.width < 100 else 26
        self.query_one("#settings_t", DataTable).add_columns("setting", "value", "applies")
        await self.load()

    async def load(self, keep_section: str | None = None) -> None:
        data = await self.c.get("/settings")
        self.settings = data["settings"]
        self.sections = []
        for s in self.settings:
            if s["section"] not in self.sections:
                self.sections.append(s["section"])
        ol = self.query_one("#sections", OptionList)
        ol.clear_options()
        for sec in self.sections:
            ol.add_option(Option(sec, id=sec))
        idx = self.sections.index(keep_section) if keep_section in self.sections else 0
        ol.highlighted = idx
        self.show_section(self.sections[idx])
        if data.get("restart_required"):
            self.flash("#msg", f"↻ restart needed for: {', '.join(data['restart_required'])}  (/restart)", ok=False)

    def show_section(self, sec: str) -> None:
        self.section = sec
        t = self.query_one("#settings_t", DataTable)
        t.clear()
        for s in self.settings:
            if s["section"] == sec:
                v = _val(s["value"])
                style = "bold" if s["value"] != s["default"] else ""
                t.add_row(Text(s["key"], style=style), v[:60], "restart" if s["apply"] == "restart" else "live", key=s["key"])
        if t.row_count:
            t.move_cursor(row=0)

    @on(OptionList.OptionHighlighted, "#sections")
    def _sec(self, ev: OptionList.OptionHighlighted) -> None:
        self.show_section(ev.option.id)

    def current(self) -> dict | None:
        t = self.query_one("#settings_t", DataTable)
        if t.cursor_row is None or not t.row_count:
            return None
        key = t.coordinate_to_cell_key(t.cursor_coordinate).row_key.value
        return next(s for s in self.settings if s["key"] == key)

    @on(DataTable.RowHighlighted, "#settings_t")
    def _row(self, _ev) -> None:
        s = self.current()
        if not s:
            return
        choices = f"  choices: {', '.join(s['choices'])}" if s["choices"] else ""
        danger = "  ⚠ security-sensitive" if s["danger"] else ""
        self.query_one("#desc", Static).update(f"{s['key']} ({s['type']}, {s['apply']}){danger}\n{s['description']}{choices}\n"
                                               f"default: {_val(s['default'])[:80]}")
        self.query_one("#value", Input).value = _val(s["value"])

    @on(DataTable.RowSelected, "#settings_t")
    def _edit(self, _ev) -> None:
        self.query_one("#value", Input).focus()

    async def on_key(self, event) -> None:
        if event.key == "space" and self.focused is self.query_one("#settings_t"):
            s = self.current()
            if s and s["type"] == "bool":
                event.stop()
                await self.save(s["key"], not s["value"])

    @on(Input.Submitted, "#value")
    async def _submit(self, ev: Input.Submitted) -> None:
        s = self.current()
        if s:
            await self.save(s["key"], ev.value)

    async def save(self, key: str, value) -> None:
        try:
            r = await self.c.post("/settings", {"key": key, "value": value})
            self.flash("#msg", f"✓ {key} = {_val(r['new'])[:60]}  ({'applied now' if r['applied'] == 'live' else 'restart required: /restart'})")
        except ClientError as e:
            self.flash("#msg", f"✗ {e}", ok=False)
        await self.load(keep_section=self.section)
        self.query_one("#settings_t", DataTable).focus()

    async def action_reload(self) -> None:
        await self.c.post("/settings/reload")
        await self.load(keep_section=self.section)
        self.flash("#msg", "reloaded forge.toml")


# ------------------------------------------------------------------------------------- models
class ModelsScreen(_Base):
    BINDINGS = _Base.BINDINGS + [Binding("u", "unpin", "unpin"), Binding("p", "pull", "pull model")]

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal"):
            yield Label("Models  ·  Enter pin · u unpin · p pull · Esc close", classes="modal-title")
            with Horizontal():
                yield OptionList(id="caps")
                yield DataTable(id="models_t", cursor_type="row")
            yield Input(placeholder="model to pull, e.g. qwen3.5:9b (Enter)", id="pull")
            yield Static("", id="msg")

    async def on_mount(self) -> None:
        self.query_one("#caps", OptionList).styles.width = 18 if self.size.width < 100 else 30
        self.query_one("#models_t", DataTable).add_columns("node", "model", "size", "params", "quant", "resident", "capabilities")
        self.query_one("#pull", Input).display = False
        await self.load()

    async def load(self) -> None:
        d = await self.c.get("/models/available")
        self.data = d
        ol = self.query_one("#caps", OptionList)
        cur = ol.highlighted or 0
        ol.clear_options()
        for cap in d["capabilities"]:
            pin = d["pins"].get(cap)
            ol.add_option(Option(Text.assemble((cap, "bold"), (f"  → {pin}" if pin else "  (auto)", "cyan" if pin else "dim")), id=cap))
        if d["capabilities"]:
            ol.highlighted = min(cur, len(d["capabilities"]) - 1)
        t = self.query_one("#models_t", DataTable)
        t.clear()
        for n in d["nodes"]:
            for m in n["models"]:
                t.add_row(n["node"] + ("" if n["healthy"] else " ✗"), m["id"], f"{m['size_mb'] / 1024:.1f}G", m.get("params") or "",
                          m.get("quant") or "", "●" if m["resident"] else "", ", ".join(m["capabilities"]) or "(not configured)",
                          key=f"{n['node']}|{m['id']}")

    def cap(self) -> str | None:
        ol = self.query_one("#caps", OptionList)
        return ol.get_option_at_index(ol.highlighted).id if ol.highlighted is not None and ol.option_count else None

    @on(DataTable.RowSelected, "#models_t")
    async def _pin(self, ev: DataTable.RowSelected) -> None:
        cap = self.cap()
        model = ev.row_key.value.split("|", 1)[1]
        if cap:
            try:
                await self.c.post("/models/pin", {"capability": cap, "model": model})
                self.flash("#msg", f"✓ {cap} now always uses {model}")
            except ClientError as e:
                self.flash("#msg", f"✗ {e}", ok=False)
            await self.load()

    async def action_unpin(self) -> None:
        cap = self.cap()
        if cap:
            await self.c.post("/models/pin", {"capability": cap, "model": None})
            self.flash("#msg", f"{cap}: automatic routing")
            await self.load()

    def action_pull(self) -> None:
        inp = self.query_one("#pull", Input)
        inp.display = True
        inp.focus()

    @on(Input.Submitted, "#pull")
    async def _do_pull(self, ev: Input.Submitted) -> None:
        if ev.value.strip():
            await self.c.post("/models/pull", {"model": ev.value.strip()})
            self.flash("#msg", f"pulling {ev.value.strip()}… (progress below)")
        ev.input.display = False

    def on_forge_event(self, ev: dict) -> None:
        if ev["kind"] == "model.pull":
            pct = f" {100 * ev['completed'] / ev['total']:.0f}%" if ev.get("total") and ev.get("completed") else ""
            self.flash("#msg", f"{ev['model']}: {ev.get('status')}{pct}", ok="error" not in str(ev.get("status")))
            if ev.get("status") == "success":
                self.run_worker(self.load())


# ------------------------------------------------------------------------------------- tools
class ToolsScreen(_Base):
    def compose(self) -> ComposeResult:
        with Vertical(classes="modal"):
            yield Label("Tools  ·  Enter toggle · type to filter · Esc close", classes="modal-title")
            yield Input(placeholder="filter", id="filter")
            yield DataTable(id="tools_t", cursor_type="row")
            yield Static("", id="msg")

    async def on_mount(self) -> None:
        self.query_one("#tools_t", DataTable).add_columns("on", "tool", "source", "side effect", "caps", "description")
        await self.load()

    async def load(self) -> None:
        self.tools = await self.c.get("/tools")
        self.render_rows()

    def render_rows(self) -> None:
        f = self.query_one("#filter", Input).value.lower()
        t = self.query_one("#tools_t", DataTable)
        cur = t.cursor_row
        t.clear()
        for x in sorted(self.tools, key=lambda x: (x["source"], x["name"])):
            if f and f not in x["name"].lower() and f not in x["description"].lower():
                continue
            se = {"none": "read-only", "local_write": "local write", "external": "EXTERNAL (approval)"}[x["side_effect"]]
            t.add_row(Text("✓", "green") if x["enabled"] else Text("✗", "red"), x["name"], x["source"], se, ",".join(x["caps"]),
                      x["description"][:60], key=x["name"])
        if t.row_count and cur is not None:
            t.move_cursor(row=min(cur, t.row_count - 1))

    @on(Input.Changed, "#filter")
    def _f(self, _ev) -> None:
        self.render_rows()

    async def toggle(self) -> None:
        t = self.query_one("#tools_t", DataTable)
        if not t.row_count:
            return
        name = t.coordinate_to_cell_key(t.cursor_coordinate).row_key.value
        tool = next(x for x in self.tools if x["name"] == name)
        try:
            await self.c.post("/tools/enable", {"pattern": name, "enabled": not tool["enabled"]})
            self.flash("#msg", f"{name} {'enabled' if not tool['enabled'] else 'disabled'}")
        except ClientError as e:
            self.flash("#msg", str(e), ok=False)
        await self.load()

    @on(DataTable.RowSelected, "#tools_t")
    async def _sel(self, _ev) -> None:
        await self.toggle()

    async def on_key(self, event) -> None:
        if event.key == "space" and self.focused is self.query_one("#tools_t"):
            event.stop()
            await self.toggle()


# ------------------------------------------------------------------------------------- sessions
class SessionsScreen(_Base):
    BINDINGS = _Base.BINDINGS + [Binding("ctrl+n", "new", "new session"),
                                Binding("ctrl+right", "next_page", "older chats", priority=True),
                                Binding("ctrl+left", "previous_page", "newer chats", priority=True)]

    def __init__(self, search: str = ""):
        super().__init__()
        self.search = search
        self.sessions: list[dict] = []
        self.page_offset = 0
        self.has_more = False

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal"):
            yield Label("Chat history  ·  Enter resume · Ctrl+N new · Esc close", classes="modal-title")
            yield Input(value=self.search, placeholder="Search title or session ID", id="filter")
            yield Static("Ctrl+Left newer chats · Ctrl+Right older chats", classes="hint")
            yield OptionList(id="sess")
            yield Static("", id="session-page", classes="hint")

    async def on_mount(self) -> None:
        self.refresh_list()
        self.query_one("#filter", Input).focus()

    @work(exclusive=True)
    async def refresh_list(self) -> None:
        self.query_one("#sess", OptionList).clear_options()
        try:
            rows = await self.c.get("/sessions", q=self.query_one("#filter", Input).value.strip(),
                                    limit=51, offset=self.page_offset)
        except ClientError as e:
            self.query_one("#session-page", Static).update(Text(str(e), style="red"))
            return
        # Older running daemons ignore q; keep filtering useful until they are restarted.
        query = self.query_one("#filter", Input).value.strip().lower()
        rows = [s for s in rows if query in (s.get("title") or "").lower() or query in s["id"].lower()]
        self.sessions, self.has_more = rows[:50], len(rows) > 50
        self.render_list()

    def render_list(self) -> None:
        ol = self.query_one("#sess", OptionList)
        ol.clear_options()
        for s in self.sessions:
            ol.add_option(Option(Text.assemble((f"{ts(s['updated_at'])}  ", "dim"), (s["title"] or "(untitled)", "bold"),
                                               (f"  {s['status']}", "yellow" if s["status"] != "idle" else "dim"), (f"  {s['id']}", "dim")), id=s["id"]))
        if ol.option_count:
            ol.highlighted = 0
        self.query_one("#session-page", Static).update(
            f"Chats {self.page_offset + 1}–{self.page_offset + len(self.sessions)}" if self.sessions else "No saved chats found.")

    def action_next_page(self) -> None:
        if self.has_more:
            self.page_offset += 50
            self.refresh_list()

    def action_previous_page(self) -> None:
        if self.page_offset:
            self.page_offset = max(0, self.page_offset - 50)
            self.refresh_list()

    @on(Input.Changed, "#filter")
    def _f(self, _ev) -> None:
        self.page_offset = 0
        self.refresh_list()

    @on(Input.Submitted, "#filter")
    def _enter(self, _ev) -> None:
        ol = self.query_one("#sess", OptionList)
        if ol.highlighted is not None and ol.option_count:
            self.dismiss(ol.get_option_at_index(ol.highlighted).id)

    @on(OptionList.OptionSelected, "#sess")
    def _sel(self, ev: OptionList.OptionSelected) -> None:
        self.dismiss(ev.option.id)

    def action_new(self) -> None:
        self.dismiss("__new__")


# ------------------------------------------------------------------------------------- approval
class ApprovalScreen(_Base):
    """Pops up when something needs the user's decision. Shows exactly what will happen."""
    BINDINGS = [Binding("a", "approve", "approve"), Binding("d", "deny", "deny"), Binding("escape", "later", "decide later")]

    def __init__(self, approval: dict):
        super().__init__()
        self.ap = approval

    def compose(self) -> ComposeResult:
        subj = json.loads(self.ap.get("subject_json") or "{}")
        kind = self.ap.get("kind")
        with Vertical(classes="modal"):
            yield Label(f"⚠ Approval needed  ·  a approve · d deny · Esc decide later (F4)", classes="modal-title")
            yield Static(Text.assemble((self.ap["title"], "bold"), f"\n{kind} · requested by {self.ap.get('requested_by')} · "
                                       f"expires {ts(self.ap.get('expires_at'))}"))
            if kind == "deploy":
                diff = subj.get("permission_diff", {})
                adv = (subj.get("verification") or {}).get("advisory_expectation_failures") or []
                yield Static(Text.assemble(("Permission changes: ", "bold"), json.dumps(diff.get("added") or {}, indent=1),
                                           ("\nRemoved: ", "bold"), json.dumps(diff.get("removed") or {}),
                                           (f"\n⚠ {len(adv)} advisory test expectation(s) failed: {adv[:3]}" if adv else "", "yellow")))
            with VerticalScroll():
                yield Static(Syntax(json.dumps(subj, indent=2, default=str)[:12000], "json", theme="ansi_dark", word_wrap=True))
            yield Input(placeholder="reason (optional) — then press a or d", id="reason")

    def on_mount(self) -> None:
        self.query_one(VerticalScroll).focus()

    def _reason(self) -> str:
        return self.query_one("#reason", Input).value

    def action_approve(self) -> None:
        if not isinstance(self.focused, Input):
            self.dismiss({"approve": True, "reason": self._reason()})

    def action_deny(self) -> None:
        if not isinstance(self.focused, Input):
            self.dismiss({"approve": False, "reason": self._reason()})

    def action_later(self) -> None:
        self.dismiss(None)

    @on(Input.Submitted, "#reason")
    def _r(self, _ev) -> None:
        self.query_one(VerticalScroll).focus()


# ------------------------------------------------------------------------------------- doctor
class DoctorScreen(_Base):
    BINDINGS = _Base.BINDINGS + [Binding("r", "rerun", "re-run")]

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal"):
            yield Label("Doctor  ·  r re-run · Esc close", classes="modal-title")
            yield DataTable(id="doc_t", cursor_type="row")
            yield Static("running checks…", id="fix", classes="hint")

    async def on_mount(self) -> None:
        self.query_one("#doc_t", DataTable).add_columns("", "check", "group", "detail")
        await self.action_rerun()

    async def action_rerun(self) -> None:
        self.checks = await self.c.get("/doctor")
        t = self.query_one("#doc_t", DataTable)
        t.clear()
        for i, ch in enumerate(self.checks):
            icon = {"ok": Text("✓", "green"), "warn": Text("!", "yellow"), "fail": Text("✗", "red")}[ch["status"]]
            t.add_row(icon, ch["name"], ch["group"], ch["detail"][:90], key=str(i))
        bad = [c for c in self.checks if c["status"] != "ok"]
        self.query_one("#fix", Static).update(f"{len(self.checks) - len(bad)}/{len(self.checks)} ok. Select a row to see its fix.")

    @on(DataTable.RowHighlighted, "#doc_t")
    def _row(self, ev: DataTable.RowHighlighted) -> None:
        if ev.row_key and ev.row_key.value is not None:
            c = self.checks[int(ev.row_key.value)]
            self.query_one("#fix", Static).update(f"{c['name']}: {c['detail']}" + (f"\nfix: {c['fix']}" if c["fix"] else ""))


# ------------------------------------------------------------------------------------- work monitor
class WorkScreen(_Base):
    BINDINGS = _Base.BINDINGS + [Binding("x", "cancel", "cancel run")]

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal"):
            yield Label("Work  ·  x cancel run · Esc close", classes="modal-title")
            yield Static("", id="summary")
            yield DataTable(id="work_runs", cursor_type="row")
            yield Label("waiting on you", classes="hint")
            yield DataTable(id="work_wait", cursor_type="row")
            yield Label("recent model calls", classes="hint")
            yield DataTable(id="work_calls", cursor_type="row")

    async def on_mount(self) -> None:
        self.query_one("#work_runs", DataTable).add_columns("run", "workflow", "trigger", "current step", "attempt", "running since")
        self.query_one("#work_wait", DataTable).add_columns("run", "workflow", "status", "waiting on")
        self.query_one("#work_calls", DataTable).add_columns("time", "model", "node", "ms", "tokens in", "tokens out")
        await self.refresh_work()
        self.set_interval(2, self.refresh_work)

    async def refresh_work(self) -> None:
        try:
            w = await self.c.get("/work")
        except ClientError:
            return
        turns = ", ".join((s.get("title") or s["id"])[:30] for s in w["agent_turns"]) or "none"
        self.query_one("#summary", Static).update(Text.assemble(("agent turns: ", "bold"), turns, ("   desktop lease: ", "bold"),
                                                                w["desktop_lease"] or "free", ("   queued runs: ", "bold"), str(w["queued"])))
        t = self.query_one("#work_runs", DataTable)
        t.clear()
        for r in w["runs"]:
            st = r.get("current_step") or {}
            t.add_row(r["id"], r.get("workflow_id", ""), r.get("trigger", ""), f"{st.get('step_path', '')} ({st.get('kind', '')})",
                      str(st.get("attempt", "")), ts(st.get("started_at") or r.get("started_at")), key=r["id"])
        t2 = self.query_one("#work_wait", DataTable)
        t2.clear()
        for r in w["waiting"]:
            t2.add_row(r["id"], r["workflow_id"], r["status"], (r["error"] or "")[:70])
        t3 = self.query_one("#work_calls", DataTable)
        t3.clear()
        for e in reversed(w["model_calls"]):
            t3.add_row(ts(e["ts"]), e.get("model", ""), e.get("node", ""), str(e.get("ms", "")), str(e.get("tokens_in", "")), str(e.get("tokens_out", "")))

    async def action_cancel(self) -> None:
        t = self.query_one("#work_runs", DataTable)
        if t.row_count:
            rid = t.coordinate_to_cell_key(t.cursor_coordinate).row_key.value
            await self.c.post(f"/runs/{rid}/cancel")
            self.app.notify(f"cancelled {rid}")


# ------------------------------------------------------------------------------------- help
class HelpScreen(_Base):
    def compose(self) -> ComposeResult:
        t = Text()
        cat = None
        for c in COMMANDS:
            if c.category != cat:
                cat = c.category
                t.append(f"\n{cat}\n", style="bold underline")
            t.append(f"  /{c.name}", style="bold")
            t.append(f" {c.args}" if c.args else "", style="dim")
            t.append(f"\n      {c.description}\n")
        t.append("\nTools / functions\n", style="bold underline")
        tools = getattr(self.app, "tools", [])
        if not tools:
            t.append("  No tools available\n", style="dim")
        for tool in sorted(tools, key=lambda x: x["name"]):
            status = "enabled" if tool.get("enabled") else "disabled"
            t.append(f"  {tool['name']}", style="bold")
            t.append(f"  [{status}]\n", style="dim")
            t.append(f"      {tool.get('description', '')}\n")
        t.append("\nKeys\n", style="bold underline")
        for k, d in KEYS:
            t.append(f"  {k:<22}", style="bold")
            t.append(f"{d}\n")
        with Vertical(classes="modal"):
            yield Label("Help  ·  Esc close", classes="modal-title")
            with VerticalScroll():
                yield Static(t)
