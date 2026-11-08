"""Clipboard for the TUI. Textual copies with OSC 52, which Windows Terminal and most SSH clients honour but the
classic console host ignores, so a local Windows session also sets the clipboard directly."""
from __future__ import annotations

import os
import sys

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002


def local_windows() -> bool:
    return sys.platform == "win32" and not os.environ.get("SSH_CONNECTION")


def set_windows_clipboard(text: str) -> bool:
    import ctypes
    import ctypes.wintypes as w
    k, u = ctypes.windll.kernel32, ctypes.windll.user32
    k.GlobalAlloc.restype, k.GlobalLock.restype = w.HGLOBAL, ctypes.c_void_p
    k.GlobalLock.argtypes = k.GlobalUnlock.argtypes = [w.HGLOBAL]
    u.SetClipboardData.argtypes = [w.UINT, w.HANDLE]
    data = text.encode("utf-16-le") + b"\0\0"
    if not u.OpenClipboard(None):
        return False
    try:
        u.EmptyClipboard()
        h = k.GlobalAlloc(GMEM_MOVEABLE, len(data))
        ptr = k.GlobalLock(h)
        ctypes.memmove(ptr, data, len(data))
        k.GlobalUnlock(h)
        return bool(u.SetClipboardData(CF_UNICODETEXT, h))   # the clipboard owns h from here
    finally:
        u.CloseClipboard()
