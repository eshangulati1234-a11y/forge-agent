"""Desktop perception: real UI state -> grounded, typed action candidates.

Primary source is the UI Automation tree (pywinauto `uia` backend). Every candidate maps to a concrete element and
the UIA patterns it supports, so a decision-maker can only choose actions that actually exist. Screenshots (mss)
are captured on demand for the vision fallback; OCR/VLM are only used when the tree is empty or unhelpful
(games, canvas apps, some Electron apps).
"""
from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

ActionKind = Literal["click", "type", "select", "toggle", "expand", "focus"]
INTERACTIVE = {"Button": "click", "MenuItem": "click", "Hyperlink": "click", "SplitButton": "click", "TabItem": "select",
               "ListItem": "select", "TreeItem": "select", "CheckBox": "toggle", "RadioButton": "select", "ComboBox": "expand",
               "Edit": "type", "Document": "type"}
READABLE = {"Text", "Edit", "Document", "ListItem", "TitleBar", "StatusBar", "Header"}
SKIP_NAMES = {"Minimize", "Maximize", "Restore", "System", "Close"}   # window chrome; Close is opt-in via allow_close


class Selector(BaseModel):
    """Replayable address of a control. Deterministic workflows store these; Laya repairs them when they drift."""
    window: str                              # window title regex
    control_type: str
    name: str | None = None
    automation_id: str | None = None
    index: int = 0

    def describe(self) -> str:
        return f"{self.control_type} '{self.name or self.automation_id}' in /{self.window}/"


class ActionCandidate(BaseModel):
    id: str
    label: str                               # meaningful slug, Laya reads it
    action: ActionKind
    control_type: str
    name: str
    automation_id: str = ""
    rect: tuple[int, int, int, int] = (0, 0, 0, 0)
    enabled: bool = True
    value: str | None = None
    parent: str = ""
    selector: Selector
    risk: Literal["low", "high"] = "low"     # high = destructive / external-looking (delete, send, submit, buy)

    def describe(self) -> str:
        verb = {"click": "click", "type": "type into", "select": "select", "toggle": "toggle", "expand": "open", "focus": "focus"}[self.action]
        where = f" (in {self.parent})" if self.parent else ""
        val = f" [current: {self.value[:40]}]" if self.value else ""
        aid = f" id:{self.automation_id}" if self.automation_id and self.automation_id != self.name else ""
        return f"{verb} {self.control_type.lower()} '{self.name or self.automation_id}'{aid}{where}{val}"


class Observation(BaseModel):
    ts: float
    window: str
    windows: list[str]
    candidates: list[ActionCandidate]
    texts: list[str] = Field(default_factory=list)   # readable state (labels, displays, document text)
    tree_size: int = 0
    observe_ms: float = 0
    screenshot: str | None = None
    digest: str = ""

    def summary(self, max_items: int = 30) -> str:
        """Compact state text for Laya / LLM prompts. Untrusted: may contain text from documents and web pages."""
        lines = [f"Active window: {self.window}"]
        if self.texts:
            lines.append("Visible text: " + " | ".join(t[:80] for t in self.texts[:12]))
        lines.append("Controls: " + ", ".join(c.describe() for c in self.candidates[:max_items]))
        return "\n".join(lines)


HIGH_RISK = re.compile(r"\b(delete|remove|send|submit|apply|purchase|buy|pay|publish|post|format|erase|uninstall|sign out|log out)\b", re.I)


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")[:32] or "control"


def list_windows() -> list[dict]:
    from pywinauto import Desktop
    out = []
    for w in Desktop(backend="uia").windows():
        try:
            t = w.window_text()
            if t and t not in ("Program Manager", "Taskbar"):
                r = w.rectangle()
                out.append({"title": t, "pid": w.element_info.process_id, "rect": (r.left, r.top, r.right, r.bottom),
                            "minimized": w.is_minimized() if hasattr(w, "is_minimized") else False})
        except Exception:
            continue
    return out


def find_window(title_re: str, timeout: float = 5.0):
    from pywinauto import Desktop
    end = time.time() + timeout
    last = None
    while time.time() < end:
        try:
            w = Desktop(backend="uia").window(title_re=title_re, visible_only=True, found_index=0)
            w.wait("exists", timeout=0.5)
            return w   # WindowSpecification: supports child_window(); attribute access resolves the wrapper
        except Exception as e:
            last = e
            time.sleep(0.2)
    from forge.errors import InterfaceChanged
    raise InterfaceChanged(f"window /{title_re}/ not found: {last}")


def observe(title_re: str, *, max_nodes: int = 400, max_depth: int = 14, screenshot_dir: Path | None = None,
            allow_close: bool = False) -> Observation:
    t0 = time.time()
    win = find_window(title_re).wrapper_object()   # resolve once; spec attribute access would re-search per call
    title = win.window_text()
    cands: list[ActionCandidate] = []
    texts: list[str] = []
    seen: dict[tuple, int] = {}
    count = 0

    def walk(el, depth: int, parent_name: str):
        nonlocal count
        if depth > max_depth or count >= max_nodes:
            return
        try:
            kids = el.children()
        except Exception:
            return
        for k in kids:
            count += 1
            try:
                ei = k.element_info
                ct, name, aid = ei.control_type or "", (ei.name or "").strip(), ei.automation_id or ""
                if not ei.visible:
                    continue
                rect = ei.rectangle
                r = (rect.left, rect.top, rect.right, rect.bottom)
            except Exception:
                continue
            if ct in READABLE and name and ct != "Edit":
                texts.append(name)
            action = INTERACTIVE.get(ct)
            if action and (name or aid) and r[2] > r[0] and r[3] > r[1]:
                if (aid in SKIP_NAMES or name.split(" ")[0] in SKIP_NAMES) and not allow_close:
                    pass
                else:
                    key = (ct, name, aid)
                    idx = seen.get(key, 0)
                    seen[key] = idx + 1
                    value = None
                    if ct in ("Edit", "Document", "ComboBox"):
                        try:
                            value = k.iface_value.CurrentValue if hasattr(k, "iface_value") else None
                        except Exception:
                            value = None
                    if ct == "CheckBox":
                        try:
                            value = "checked" if k.get_toggle_state() == 1 else "unchecked"
                        except Exception:
                            pass
                    enabled = bool(getattr(ei, "enabled", True))
                    cid = f"c{len(cands)}"
                    label = _slug(f"{action}_{name or aid}")
                    if any(c.label == label for c in cands):
                        label = f"{label}_{len(cands)}"
                    cands.append(ActionCandidate(
                        id=cid, label=label, action=action, control_type=ct, name=name, automation_id=aid, rect=r, enabled=enabled,
                        value=value, parent=parent_name, risk="high" if HIGH_RISK.search(name) else "low",
                        selector=Selector(window=re.escape(title), control_type=ct, name=name or None, automation_id=aid or None, index=idx)))
            walk(k, depth + 1, name if ct in ("Group", "Pane", "Menu", "MenuBar", "List", "Tab", "ToolBar") and name else parent_name)

    walk(win, 0, "")
    cands = [c for c in cands if c.enabled]
    shot = None
    if screenshot_dir:
        shot = str(capture(win, screenshot_dir))
    digest = hashlib.sha1(("|".join(texts) + "|".join(f"{c.label}:{c.value}" for c in cands)).encode()).hexdigest()[:12]
    try:
        windows = [w["title"] for w in list_windows()][:20]
    except Exception:
        windows = [title]
    return Observation(ts=time.time(), window=title, windows=windows, candidates=cands, texts=texts, tree_size=count,
                       observe_ms=round((time.time() - t0) * 1000, 1), screenshot=shot, digest=digest)


def capture(win, out_dir: Path) -> Path:
    import mss
    import mss.tools
    out_dir.mkdir(parents=True, exist_ok=True)
    r = win.rectangle()
    region = {"left": r.left, "top": r.top, "width": max(1, r.width()), "height": max(1, r.height())}
    path = out_dir / f"shot_{int(time.time() * 1000)}.png"
    with mss.mss() as m:
        img = m.grab(region)
        mss.tools.to_png(img.rgb, img.size, output=str(path))
    return path
