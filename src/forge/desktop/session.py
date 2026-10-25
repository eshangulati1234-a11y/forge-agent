"""Windows interactive-session facts and the desktop lease.

Reality check (documented in docs/ARCHITECTURE.md):
  * UI Automation only works inside an interactive, unlocked desktop session. A Windows service runs in Session 0
    and cannot see or drive the user's desktop, so forged runs as a logon task in the user's session.
  * On a locked workstation or a disconnected/minimised RDP session, input injection fails and many UIA calls
    return stale data. GUI tasks therefore defer while locked.
  * UAC consent prompts appear on the Secure Desktop; no unprivileged process can automate them (by design).
    Forge never runs elevated and treats a UAC prompt as needs_user.
  * One physical desktop = one GUI task at a time (the lease). Truly unattended parallel GUI work needs a
    separate session (a Hyper-V VM or a second Windows user over RDP) - phase 2.
"""
from __future__ import annotations

import asyncio
import ctypes
import ctypes.wintypes as wt
import time
from contextlib import asynccontextmanager

from forge.errors import NeedsUser, ResourceUnavailable

user32 = ctypes.windll.user32
DESKTOP_SWITCHDESKTOP = 0x0100


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.UINT), ("dwTime", wt.DWORD)]


def idle_seconds() -> float:
    info = LASTINPUTINFO(cbSize=ctypes.sizeof(LASTINPUTINFO))
    user32.GetLastInputInfo(ctypes.byref(info))
    return (ctypes.windll.kernel32.GetTickCount() - info.dwTime) / 1000.0


def is_locked() -> bool:
    """True when the input desktop is not the default one (lock screen, UAC secure desktop, Ctrl+Alt+Del)."""
    h = user32.OpenInputDesktop(0, False, DESKTOP_SWITCHDESKTOP)
    if not h:
        return True
    try:
        return not user32.SwitchDesktop(h)
    finally:
        user32.CloseDesktop(h)


def uac_prompt_visible() -> bool:
    import psutil
    return any(p.info["name"] and p.info["name"].lower() == "consent.exe" for p in psutil.process_iter(["name"]))


def display_info() -> dict:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)   # per-monitor DPI aware: rectangles are physical pixels
    except Exception:
        pass
    import mss
    with mss.mss() as m:
        mons = [{"left": x["left"], "top": x["top"], "width": x["width"], "height": x["height"]} for x in m.monitors[1:]]
    dpi = user32.GetDpiForSystem() if hasattr(user32, "GetDpiForSystem") else 96
    return {"monitors": mons, "scale": round(dpi / 96, 2), "primary": mons[0] if mons else None}


class DesktopLease:
    """Exclusive use of the interactive desktop, gated on lock state and user activity."""

    def __init__(self, idle_required_s: float = 120):
        self.idle_required_s = idle_required_s
        self._lock = asyncio.Lock()
        self.holder: str | None = None

    def check(self, *, attended: bool) -> None:
        if is_locked():
            raise ResourceUnavailable("desktop is locked; GUI step deferred until the user unlocks")
        if uac_prompt_visible():
            raise NeedsUser("a UAC prompt is on screen; Forge cannot and will not answer it")
        if not attended and idle_seconds() < self.idle_required_s:
            raise ResourceUnavailable(f"user active in the last {self.idle_required_s:.0f}s; unattended GUI step deferred")

    @asynccontextmanager
    async def hold(self, holder: str, *, attended: bool, wait_s: float = 30):
        self.check(attended=attended)
        try:
            await asyncio.wait_for(self._lock.acquire(), wait_s)
        except asyncio.TimeoutError:
            raise ResourceUnavailable(f"desktop is leased by {self.holder}") from None
        self.holder = holder
        t = time.time()
        try:
            yield
        finally:
            self.holder = None
            self._lock.release()
            _ = t
