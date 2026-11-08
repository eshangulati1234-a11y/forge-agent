"""Slash command and tool search used by autocomplete and help."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Command:
    name: str
    args: str
    category: str
    description: str


@dataclass(frozen=True)
class ToolMatch:
    name: str
    description: str
    enabled: bool


COMMANDS: list[Command] = [
    Command("chat", "", "Views", "return to the conversation · F1"),
    Command("runs", "", "Views", "inspect runs and their results · F2"),
    Command("approvals", "", "Views", "review pending approvals · F4"),
    Command("records", "", "Views", "browse saved records · F5"),
    Command("memory", "", "Views", "browse stored memories · F6"),
    Command("activity", "", "Views", "inspect detailed events and tool activity · F7"),
    # session
    Command("new", "", "Session", "start a new chat session"),
    Command("sessions", "", "Session", "switch session (picker)  · Ctrl+X"),
    Command("history", "[search]", "Session", "browse saved chats by title or session ID"),
    Command("resume", "[session ID or search]", "Session", "reopen a saved chat; omit arguments for the history picker"),
    Command("title", "<name>", "Session", "rename this session"),
    Command("interrupt", "", "Session", "stop the agent's current turn  · Ctrl+C on empty prompt"),
    Command("queue", "[clear]", "Session", "show or clear messages queued while the agent is busy"),
    Command("copy", "[n]", "Session", "copy Forge's last reply (or the nth from last) to the clipboard"),
    Command("compact", "", "Session", "summarise older messages so they stop filling the model context"),
    # models
    Command("model", "", "Models", "model picker: see nodes, pin a model to a capability, pull models"),
    Command("pin", "<capability> [model]", "Models", "pin (or unpin) a model for a capability"),
    # system
    Command("settings", "", "System", "settings editor  · Ctrl+O"),
    Command("set", "<key> <value>", "System", "change one setting (validated, audited)"),
    Command("tools", "", "System", "enable / disable tools"),
    Command("doctor", "", "System", "health checks with fixes"),
    Command("work", "", "System", "live work monitor  · Ctrl+T"),
    Command("status", "[raw]", "System", "context left, loaded models, GPU and daemon health"),
    Command("restart", "", "System", "restart the background daemon"),
    Command("reload", "", "System", "re-read forge.toml into the running daemon"),
    Command("theme", "[name]", "System", "switch colour theme (persists)"),
    # automation
    Command("workflows", "", "Automation", "manage workflows · F3"),
    Command("compile", "<request>", "Automation", "build a workflow from a description"),
    Command("run", "<workflow> [json inputs]", "Automation", "run a workflow now"),
    Command("disable", "<workflow>", "Automation", "switch a workflow off (schedules removed)  · d in Workflows tab"),
    Command("enable", "<workflow>", "Automation", "re-deploy a disabled workflow (asks for approval)"),
    Command("approve", "<id> [reason]", "Automation", "approve a pending request"),
    Command("deny", "<id> [reason]", "Automation", "deny a pending request"),
    Command("export", "<collection> <path.xlsx>", "Automation", "export records to Excel"),
    Command("remember", "<fact>", "Automation", "store a user-stated fact (trusted memory)"),
    # safety
    Command("stop", "", "Safety", "EMERGENCY STOP all actions  · Ctrl+E Ctrl+E"),
    Command("unstop", "", "Safety", "clear the emergency stop and re-enable actions"),
    Command("help", "", "Help", "this help"),
    Command("quit", "", "Help", "close the TUI (Forge keeps running)"),
]
BY_NAME = {c.name: c for c in COMMANDS}

KEYS = [
    ("Esc", "close suggestions / dialog / return to chat"),
    ("Enter", "send / accept suggestion"), ("Shift+Tab · Alt+Enter · Ctrl+J", "new line"), ("Tab", "accept suggestion"),
    ("Up / Down", "previous / next prompt at the first / last line; Down restores your draft"),
    ("Mouse drag", "select text in the conversation"), ("Ctrl+C", "copy the selection; otherwise clear draft, then interrupt"),
    ("Ctrl+D", "quit on empty prompt"),
    ("Ctrl+O", "settings"), ("Ctrl+T", "work monitor"), ("Ctrl+X", "session picker"), ("Ctrl+P", "command palette"),
    ("F1–F7", "Chat · Runs · Workflows · Approvals · Records · Memory · Activity"), ("Ctrl+E ×2", "EMERGENCY STOP"),
    ("Ctrl+Q", "quit (Forge keeps running)"),
]


def matches(prefix: str, tools: list[dict] | None = None) -> list[Command | ToolMatch]:
    """Search command and tool names/descriptions, keeping commands ahead of tools."""
    p = prefix.lstrip("/").strip().lower()
    if not p:
        return list(COMMANDS)

    def rank(name: str, description: str) -> int | None:
        name, description = name.lower(), description.lower()
        if name.startswith(p):
            return 0
        if p in name:
            return 1
        if p in description:
            return 2
        return None

    command_hits = [(rank(c.name, c.description), c) for c in COMMANDS]
    tool_hits = [(rank(t["name"], t.get("description", "")),
                  ToolMatch(t["name"], t.get("description", ""), bool(t.get("enabled", False))))
                 for t in (tools or [])]
    command_hits = [(r, c) for r, c in command_hits if r is not None]
    tool_hits = [(r, t) for r, t in tool_hits if r is not None]
    return [x for _, x in sorted(command_hits, key=lambda x: x[0])] + \
           [x for _, x in sorted(tool_hits, key=lambda x: (x[0], x[1].name))]
