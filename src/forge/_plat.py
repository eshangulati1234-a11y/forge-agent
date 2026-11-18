"""Small cross-platform helpers: Windows-only subprocess flags must not be passed on other platforms."""
from __future__ import annotations

import sys

WIN = sys.platform == "win32"
MAC = sys.platform == "darwin"
CREATE_NO_WINDOW = 0x08000000
DETACHED_PROCESS = 0x00000008


def win_flags(flags: int = CREATE_NO_WINDOW) -> dict:
    """kwargs for subprocess: creationflags on Windows, nothing elsewhere."""
    return {"creationflags": flags} if WIN else {}
