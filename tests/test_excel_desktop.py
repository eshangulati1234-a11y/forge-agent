from types import SimpleNamespace

import pytest
from openpyxl import load_workbook

from forge.errors import InvalidInput


def test_create_workbook_without_excel_and_never_overwrite(services, tmp_path):
    from forge.records.excel import create_workbook
    path = tmp_path / "jobs.xlsx"
    rows = [["Junior Developer", "https://example.org/job", "=literal text", None], ["Engineer", "", 2, True]]
    result = create_workbook(path, "Jobs", ["Title", "URL", "Match", "Notes"], rows)
    assert result["verified"] and result["rows"] == 2
    wb = load_workbook(path)
    try:
        assert wb["Jobs"]["C2"].data_type == "s"
        assert wb["Jobs"]["C2"].value == "=literal text"
        assert wb["Jobs"].freeze_panes == "A2"
    finally:
        wb.close()
    original = path.read_bytes()
    with pytest.raises(InvalidInput, match="already exists"):
        create_workbook(path, "Jobs", ["Title"], [])
    assert path.read_bytes() == original
    with pytest.raises(InvalidInput, match="same number"):
        create_workbook(tmp_path / "bad.xlsx", "Jobs", ["Title"], [[1, 2]])


def test_office_registry_lookup_and_workbook_launch(monkeypatch, tmp_path):
    import winreg
    from forge.desktop import launch
    executable = tmp_path / "Office Folder" / "EXCEL.EXE"
    executable.parent.mkdir()
    executable.touch()
    workbook = tmp_path / "job list.xlsx"
    workbook.touch()
    monkeypatch.setattr(launch.shutil, "which", lambda _: None)
    monkeypatch.setattr(launch.sys, "platform", "win32")

    class Key:
        def __enter__(self): return self
        def __exit__(self, *args): pass

    monkeypatch.setattr(winreg, "OpenKey", lambda *args: Key())
    monkeypatch.setattr(winreg, "QueryValueEx", lambda *args: (str(executable), 1))
    calls = []
    monkeypatch.setattr(launch.subprocess, "Popen", lambda argv, **kwargs: calls.append((argv, kwargs)) or SimpleNamespace(pid=123))
    assert launch.resolve_executable("excel.exe") == str(executable)
    result = launch.open_workbook(workbook)
    assert result["launched"] and calls[0][0] == [str(executable), str(workbook)]


def test_app_window_lookup_matches_title_suffix_and_respects_anchors(monkeypatch):
    import pywinauto
    from forge.desktop.perceive import find_window
    from forge.errors import InterfaceChanged
    window = SimpleNamespace(wait=lambda *args, **kwargs: None)

    def select(**criteria):
        if criteria["predicate_func"](SimpleNamespace(name="Untitled - Notepad")):
            return window
        raise RuntimeError("no matching title")

    monkeypatch.setattr(pywinauto, "Desktop", lambda **kwargs: SimpleNamespace(window=select))
    assert find_window("Notepad") is window
    assert find_window("(?i)notepad") is window
    with pytest.raises(InterfaceChanged):
        find_window("^Notepad$", timeout=.01)
