"""Windows app lookup includes App Paths, where Office registers executables outside PATH."""
from __future__ import annotations

import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from forge.errors import InvalidInput


def resolve_executable(name: str) -> str:
    if Path(name).is_file():
        return str(Path(name).resolve())
    found = shutil.which(name)
    if found:
        return found
    if sys.platform == "win32" and Path(name).name == name:
        import winreg
        exe = name if name.lower().endswith(".exe") else name + ".exe"
        for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
                try:
                    with winreg.OpenKey(root, rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{exe}",
                                        0, winreg.KEY_READ | view) as key:
                        registered = str(winreg.QueryValueEx(key, "")[0]).strip('"')
                        if Path(registered).is_file():
                            return registered
                except OSError:
                    continue
    raise InvalidInput(f"Could not locate executable {name!r} via PATH or Windows App Paths. "
                       "Supply its full path; this does not establish whether the application is installed.")


def launch_app(command: str):
    if Path(command).is_file():
        argv = [command]
    else:
        argv = [part.strip('"') for part in shlex.split(command, posix=sys.platform != "win32")]
    if not argv:
        raise InvalidInput("application command is empty")
    argv[0] = resolve_executable(argv[0])
    return subprocess.Popen(argv, shell=False)


def open_workbook(path: Path) -> dict:
    path = path.expanduser().resolve()
    if not path.is_file() or path.suffix.lower() != ".xlsx":
        raise InvalidInput("excel.open needs an existing .xlsx workbook")
    process = subprocess.Popen([resolve_executable("excel.exe"), str(path)], shell=False)
    return {"path": str(path), "launched": True, "pid": process.pid}
