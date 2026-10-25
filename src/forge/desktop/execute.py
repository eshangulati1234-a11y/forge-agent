"""Desktop action executor. Pattern-first: UIA Invoke/Value/Toggle/Select/ExpandCollapse act on the control without
moving the user's mouse or stealing keyboard focus. Real input (click_input/type_keys) is the fallback and is
reported as `method="input"` so benchmarks can count how often the fallback was needed.
"""
from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass

from forge.desktop.perceive import ActionCandidate, Selector, find_window
from forge.errors import InterfaceChanged, InvalidInput


@dataclass
class ActionResult:
    ok: bool
    method: str            # pattern | input
    ms: float
    detail: str = ""


def resolve(sel: Selector):
    win = find_window(sel.window)
    crit = {"control_type": sel.control_type}
    if sel.automation_id:
        crit["auto_id"] = sel.automation_id
    if sel.name:
        crit["title"] = sel.name
    try:
        spec = win.child_window(found_index=sel.index, **crit) if sel.index else win.child_window(**crit)
        return spec.wrapper_object()
    except Exception as e:
        if sel.automation_id and sel.name:  # name changed (e.g. localisation / dynamic label): retry by automation id only
            try:
                return win.child_window(control_type=sel.control_type, auto_id=sel.automation_id).wrapper_object()
            except Exception:
                pass
        raise InterfaceChanged(f"control not found: {sel.describe()} ({type(e).__name__})") from None


def act(target: ActionCandidate | Selector, action: str | None = None, text: str | None = None) -> ActionResult:
    sel = target.selector if isinstance(target, ActionCandidate) else target
    action = action or (target.action if isinstance(target, ActionCandidate) else "click")
    t0 = time.time()
    el = resolve(sel)
    method = "pattern"
    try:
        if action == "click":
            try:
                el.invoke()
            except Exception:
                method = "input"
                el.click_input()
        elif action == "type":
            if text is None:
                raise InvalidInput("type action needs text")
            try:
                el.iface_value.SetValue(text)
            except Exception:
                method = "input"
                el.set_focus()
                el.type_keys(_escape(text), with_spaces=True, with_newlines=True, pause=0.01)
        elif action == "toggle":
            el.toggle()
        elif action == "select":
            try:
                el.select()
            except Exception:
                try:
                    el.invoke()
                except Exception:
                    method = "input"
                    el.click_input()
        elif action == "expand":
            try:
                el.expand()
            except Exception:
                method = "input"
                el.click_input()
        elif action == "focus":
            el.set_focus()
            method = "input"
        else:
            raise InvalidInput(f"unknown action {action}")
    except (InterfaceChanged, InvalidInput):
        raise
    except Exception as e:
        return ActionResult(False, method, (time.time() - t0) * 1000, f"{type(e).__name__}: {e}")
    return ActionResult(True, method, (time.time() - t0) * 1000)


def _escape(text: str) -> str:
    # pywinauto send_keys treats + ^ % ~ ( ) { } as modifiers/specials
    return "".join("{" + ch + "}" if ch in "+^%~(){}[]" else ch for ch in text)


def hotkey(window_re: str, keys: str) -> ActionResult:
    """keys in pywinauto syntax, e.g. '^s' (Ctrl+S), '%{F4}'. Requires focus: uses real input."""
    t0 = time.time()
    w = find_window(window_re)
    w.set_focus()
    w.type_keys(keys)
    return ActionResult(True, "input", (time.time() - t0) * 1000)


def open_app(command: str, window_re: str, timeout: float = 15) -> dict:
    t0 = time.time()
    subprocess.Popen(command, shell=False)
    w = find_window(window_re, timeout=timeout)
    return {"window": w.window_text(), "ms": round((time.time() - t0) * 1000)}


def close_window(window_re: str) -> None:
    try:
        find_window(window_re, timeout=1).close()
    except Exception:
        pass


def read_text(sel: Selector) -> str:
    el = resolve(sel)
    try:
        return el.iface_value.CurrentValue
    except Exception:
        return el.window_text()


def clipboard_set(text: str) -> None:
    import win32clipboard
    win32clipboard.OpenClipboard()
    try:
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardText(text, win32clipboard.CF_UNICODETEXT)
    finally:
        win32clipboard.CloseClipboard()


def clipboard_get() -> str:
    import win32clipboard
    win32clipboard.OpenClipboard()
    try:
        return win32clipboard.GetClipboardData(win32clipboard.CF_UNICODETEXT)
    except TypeError:
        return ""
    finally:
        win32clipboard.CloseClipboard()


def file_dialog_choose(path: str, dialog_re: str = "Open|Save As|Choose|Upload|File Upload") -> ActionResult:
    """Fill a standard Windows file dialog's 'File name' box and confirm."""
    t0 = time.time()
    d = find_window(dialog_re, timeout=10)
    box = d.child_window(control_type="Edit", title="File name:").wrapper_object()
    box.iface_value.SetValue(path)
    for name in ("Open", "Save", "OK"):
        try:
            d.child_window(control_type="Button", title=name, found_index=0).wrapper_object().invoke()
            return ActionResult(True, "pattern", (time.time() - t0) * 1000)
        except Exception:
            continue
    box.type_keys("{ENTER}")
    return ActionResult(True, "input", (time.time() - t0) * 1000)


def scroll(window_re: str, amount: int = -5) -> ActionResult:
    t0 = time.time()
    w = find_window(window_re)
    w.scroll("down" if amount < 0 else "up", "line", abs(amount))
    return ActionResult(True, "input", (time.time() - t0) * 1000)


def drag(window_re: str, src: tuple[int, int], dst: tuple[int, int]) -> ActionResult:
    from pywinauto import mouse
    t0 = time.time()
    find_window(window_re).set_focus()
    mouse.press(coords=src)
    mouse.move(coords=dst)
    mouse.release(coords=dst)
    return ActionResult(True, "input", (time.time() - t0) * 1000)
