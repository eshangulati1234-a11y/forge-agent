"""Single source of truth for slash commands: feeds autocomplete, /help and the Ctrl+P command palette."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Command:
    name: str
    args: str
    category: str
    description: str


COMMANDS: list[Command] = [
    # session
    Command("new", "", "Session", "start a new chat session"),
    Command("sessions", "", "Session", "switch session (picker)  · Ctrl+X"),
    Command("title", "<name>", "Session", "rename this session"),
    Command("interrupt", "", "Session", "stop the agent's current turn  · Ctrl+C on empty prompt"),
    Command("queue", "[clear]", "Session", "show or clear messages queued while the agent is busy"),
    # models
    Command("model", "", "Models", "model picker: see nodes, pin a model to a capability, pull models"),
    Command("pin", "<capability> [model]", "Models", "pin (or unpin) a model for a capability"),
    # system
    Command("settings", "", "System", "settings editor  · Ctrl+O"),
    Command("set", "<key> <value>", "System", "change one setting (validated, audited)"),
    Command("tools", "", "System", "enable / disable tools"),
    Command("doctor", "", "System", "health checks with fixes"),
    Command("work", "", "System", "live work monitor  · Ctrl+T"),
    Command("status", "", "System", "raw daemon status"),
    Command("restart", "", "System", "restart the background daemon"),
    Command("reload", "", "System", "re-read forge.toml into the running daemon"),
    Command("theme", "[name]", "System", "switch colour theme (persists)"),
    # automation
    Command("workflows", "", "Automation", "list workflows"),
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
    Command("resume", "", "Safety", "clear the emergency stop"),
    Command("help", "", "Help", "this help"),
    Command("quit", "", "Help", "close the TUI (Forge keeps running)"),
]
BY_NAME = {c.name: c for c in COMMANDS}

KEYS = [
    ("Enter", "send / accept suggestion"), ("Alt+Enter · Ctrl+J", "new line"), ("Tab", "accept suggestion"),
    ("Ctrl+C", "clear draft, then interrupt the agent"), ("Ctrl+D", "quit on empty prompt"),
    ("Ctrl+O", "settings"), ("Ctrl+T", "work monitor"), ("Ctrl+X", "session picker"), ("Ctrl+P", "command palette"),
    ("F1–F7", "Chat · Runs · Workflows · Approvals · Records · Memory · Activity"), ("Ctrl+E ×2", "EMERGENCY STOP"),
    ("Ctrl+Q", "quit (Forge keeps running)"),
]


def matches(prefix: str) -> list[Command]:
    p = prefix.lstrip("/").lower()
    starts = [c for c in COMMANDS if c.name.startswith(p)]
    contains = [c for c in COMMANDS if p and p in c.name and c not in starts]
    return starts + contains
