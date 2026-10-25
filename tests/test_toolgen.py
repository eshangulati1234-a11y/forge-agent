from pathlib import Path

import pytest

from forge.errors import ForgeError
from forge.tools.toolgen import run_isolated, static_check

GOOD = '''
import re
def run(args):
    m = re.findall(r"\\$?(\\d[\\d,]*)", args["text"])
    nums = [int(x.replace(",", "")) for x in m]
    return {"min": min(nums) if nums else None, "max": max(nums) if nums else None}
def test_range():
    assert run({"text": "$70,000 - $85,000"}) == {"min": 70000, "max": 85000}
def test_none():
    assert run({"text": "competitive"}) == {"min": None, "max": None}
'''


def test_static_check_blocks_io_and_reflection():
    assert static_check(GOOD) == []
    bad = "import os\ndef run(a):\n    return open('x').read()\ndef test_x():\n    pass"
    probs = static_check(bad)
    assert any("os" in p for p in probs) and any("open" in p for p in probs)
    assert static_check("def run(a):\n    return ().__class__.__bases__\ndef test_a(): pass")


def test_isolated_tests_and_call(tmp_path):
    p = tmp_path / "tool.py"
    p.write_text(GOOD)
    assert all(r["ok"] for r in run_isolated(p, "test")["results"])
    assert run_isolated(p, "run", {"text": "$60,000 to $72,500"})["result"] == {"min": 60000, "max": 72500}


def test_isolation_timeout_and_no_env(tmp_path):
    p = tmp_path / "tool.py"
    p.write_text("def run(a):\n    while True:\n        pass\ndef test_x(): pass")
    with pytest.raises(ForgeError, match="exceeded"):
        run_isolated(p, "run", {})
