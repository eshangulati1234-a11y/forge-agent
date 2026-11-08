"""Make Forge's own browser windows visible on Windows.

Playwright starts its driver with SW_HIDE and the driver starts the browser with windowsHide, so a "visible"
(headed) Brave/Chrome can open its first window hidden: the page loads and forms get filled in a window the
user cannot see. After launch we show the top-level windows of the browser process that owns this profile's
user-data-dir. Only that process is touched, never the user's own browser.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

log = logging.getLogger(__name__)

WS_CAPTION = 0x00C00000
GW_OWNER = 4
SW_SHOWNORMAL = 1


def profile_pids(user_data_dir: Path) -> set[int]:
    import psutil
    want = str(user_data_dir).lower().rstrip("\\/")
    pids = set()
    for p in psutil.process_iter(["cmdline"]):
        for arg in p.info["cmdline"] or []:
            if arg.lower().startswith("--user-data-dir=") and arg.split("=", 1)[1].strip('"').lower().rstrip("\\/") == want:
                pids.add(p.pid)
                break
    return pids


def active_tab_titles(pids: set[int]) -> list[str]:
    """Title of the selected tab in each visible browser window ("Jobs - Brave" -> "Jobs"). Playwright makes every
    tab report visible and focused, so the window title is the only reliable signal of the tab the user is on."""
    if sys.platform != "win32" or not pids:
        return []
    import ctypes
    import ctypes.wintypes as w
    u = ctypes.windll.user32
    u.GetWindowLongW.restype = ctypes.c_long
    titles = []

    @ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)
    def visit(hwnd, _):
        pid = w.DWORD()
        u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        n = u.GetWindowTextLengthW(hwnd)
        if (pid.value in pids and n and u.IsWindowVisible(hwnd) and u.GetWindowLongW(hwnd, -16) & WS_CAPTION == WS_CAPTION
                and not u.GetWindow(hwnd, GW_OWNER)):
            buf = ctypes.create_unicode_buffer(n + 1)
            u.GetWindowTextW(hwnd, buf, n + 1)
            titles.append(buf.value.rsplit(" - ", 1)[0] if " - " in buf.value else buf.value)
        return True

    u.EnumWindows(visit, 0)
    return titles


def reveal(pids: set[int]) -> int:
    """Show hidden main windows owned by `pids` (from profile_pids). Returns how many were shown."""
    if sys.platform != "win32" or not pids:
        return 0
    import ctypes
    import ctypes.wintypes as w
    u = ctypes.windll.user32
    u.GetWindowLongW.restype = ctypes.c_long
    hidden = []

    @ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)
    def visit(hwnd, _):
        pid = w.DWORD()
        u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if (pid.value in pids and not u.IsWindowVisible(hwnd) and u.GetWindowTextLengthW(hwnd) > 0
                and u.GetWindowLongW(hwnd, -16) & WS_CAPTION == WS_CAPTION and not u.GetWindow(hwnd, GW_OWNER)):
            hidden.append(hwnd)
        return True

    u.EnumWindows(visit, 0)
    for hwnd in hidden:
        u.ShowWindow(hwnd, SW_SHOWNORMAL)
        u.SetForegroundWindow(hwnd)   # Windows may refuse focus to a background process; the window still shows
    if hidden:
        log.info("showed %d hidden browser window(s)", len(hidden))
    return len(hidden)
