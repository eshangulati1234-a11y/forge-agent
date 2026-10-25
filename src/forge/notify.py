"""Notifications: always an event (TUI shows it); optionally a Windows toast."""
from __future__ import annotations

import logging
import subprocess
import sys

log = logging.getLogger("forge.notify")

_TOAST_PS = r"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] > $null
$t = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
$x = $t.GetElementsByTagName('text'); $x.Item(0).AppendChild($t.CreateTextNode($env:FORGE_T)) > $null; $x.Item(1).AppendChild($t.CreateTextNode($env:FORGE_B)) > $null
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('Forge').Show([Windows.UI.Notifications.ToastNotification]::new($t))
"""


def toast(title: str, body: str) -> None:
    if sys.platform != "win32":
        return
    import os
    env = {**os.environ, "FORGE_T": title[:120], "FORGE_B": body[:240]}  # passed via env: no injection into the script
    try:
        subprocess.Popen(["powershell", "-NoProfile", "-NonInteractive", "-Command", _TOAST_PS], env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=0x08000000)
    except Exception as e:
        log.debug("toast failed: %s", e)
