import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from forge.workflows.ir import WorkflowIR, check_ir
from forge.workflows.registry import CompileError
from tests.helpers import deploy


def wf(tmp, **over):
    base = {
        "id": "test.pipeline", "name": "Pipeline",
        "inputs": {"items": {"type": "array", "default": [1, 2, 3]}},
        "permissions": {"tools": ["test.*"], "fs_read": [str(tmp)], "fs_write": [str(tmp)]},
        "steps": [
            {"kind": "set", "id": "prep", "values": {"doubled": "{{ inputs['items'] | map('int') | map('string') | list }}"}},
            {"kind": "foreach", "id": "loop", "items": "inputs['items']", "steps": [
                {"kind": "tool", "id": "e", "tool": "test.echo", "args": {"value": "{{ item * 10 }}"}}]},
            {"kind": "branch", "id": "big", "condition": "inputs['items'] | length > 2",
             "then": [{"kind": "tool", "id": "yes", "tool": "test.echo", "args": {"value": "many"}}],
             "else": [{"kind": "tool", "id": "no", "tool": "test.echo", "args": {"value": "few"}}]},
        ],
        "outputs": {"values": "{{ steps.loop | map(attribute='e') | map(attribute='value') | list }}", "branch": "{{ steps.big.taken }}"},
        "tests": [{"name": "basic", "expect": ["status == 'succeeded'", "outputs['values'] == [10, 20, 30]"]}],
    }
    base.update(over)
    return base


def test_static_check_reports_structured_errors(services, tmp_path):
    bad = wf(tmp_path, steps=[
        {"kind": "tool", "id": "a", "tool": "test.nope", "args": {}},
        {"kind": "tool", "id": "b", "tool": "test.send", "args": {"to": "x"}},
        {"kind": "tool", "id": "c", "tool": "test.echo", "args": {"value": "{{ steps.ghost.x }}"}},
        {"kind": "tool", "id": "d", "tool": "fs.write_text", "args": {"path": "x", "text": "y"}},
    ])
    codes = {i.code for i in check_ir(WorkflowIR.model_validate(bad), services.registry)}
    assert {"unknown_tool", "missing_arg", "unknown_step_ref", "tool_not_granted"} <= codes
    with pytest.raises(CompileError):
        services.workflows.create_version(WorkflowIR.model_validate(bad), "user")


async def test_run_foreach_branch_outputs(services, tmp_path):
    deploy(services, wf(tmp_path))
    rid = services.runtime.enqueue("test.pipeline")
    run = await services.runtime.execute_now(rid)
    assert run["status"] == "succeeded", run["error"]
    out = json.loads(run["outputs_json"])
    assert out == {"values": [10, 20, 30], "branch": "then"}


async def test_workflow_tests_and_dry_run(services, tmp_path):
    v = services.workflows.create_version(WorkflowIR.model_validate(wf(tmp_path)), "user")
    res = await services.workflows.run_tests(v["id"])
    assert res[0]["passed"], res
    dry = await services.workflows.dry_run(v["id"])
    assert dry["status"] == "succeeded"


async def test_transient_retry_then_success(services, tmp_path):
    deploy(services, wf(tmp_path, id="test.flaky", steps=[
        {"kind": "tool", "id": "f", "tool": "test.flaky", "args": {"fail_times": 2, "path": str(tmp_path / "flaky.txt")},
         "retry": {"max_attempts": 3, "backoff_s": 0.01}}], outputs={}, tests=[]))
    run = await services.runtime.execute_now(services.runtime.enqueue("test.flaky"))
    assert run["status"] == "succeeded"
    assert services.db.one("SELECT attempt FROM step_runs WHERE run_id=?", (run["id"],))["attempt"] == 3


async def test_approval_step_pauses_and_resumes(services, tmp_path):
    deploy(services, wf(tmp_path, id="test.approve", steps=[
        {"kind": "approval", "id": "ok", "title": "send it?"},
        {"kind": "tool", "id": "after", "tool": "test.echo", "args": {"value": "{{ steps.ok.approved }}"}}],
        outputs={"v": "{{ steps.after.value }}"}, tests=[]))
    rid = services.runtime.enqueue("test.approve")
    run = await services.runtime.execute_now(rid)
    assert run["status"] == "waiting_approval"
    ap = services.approvals.pending()[0]
    services.workflows.handle_approval(services.approvals.decide(ap["id"], True))
    assert services.db.one("SELECT status FROM runs WHERE id=?", (rid,))["status"] == "queued"
    run = await services.runtime.execute_now(rid)
    assert run["status"] == "succeeded" and json.loads(run["outputs_json"]) == {"v": True}


async def test_dedupe_key(services, tmp_path):
    deploy(services, wf(tmp_path))
    assert services.runtime.enqueue("test.pipeline", dedupe_key="k1")
    assert services.runtime.enqueue("test.pipeline", dedupe_key="k1") is None


async def test_scheduler_fires_interval_once_per_slot(services, tmp_path):
    deploy(services, wf(tmp_path, triggers=[{"type": "interval", "every_s": 60}]))
    services.db.execute("UPDATE schedules SET next_fire_at=?", (time.time() - 1,))
    services.scheduler.tick()
    services.scheduler.tick()
    assert services.db.one("SELECT COUNT(*) n FROM runs WHERE trigger='interval'")["n"] == 1


CRASH_SCRIPT = r"""
import asyncio, sys, os
sys.path.insert(0, sys.argv[2])
from forge.config import load_config
from forge.db import DB
from forge.services import Services
async def main():
    cfg = load_config(); cfg.notify_toast = False
    s = Services(cfg=cfg, db=DB(), approval_key=b"k"*32, laya=False); s.load_builtin_tools()
    import tests.helpers
    await s.runtime.execute_now(sys.argv[1])
asyncio.run(main())
"""


async def test_crash_recovery_resumes_from_checkpoint(services, home, tmp_path):
    """Kill a worker process mid-run; a new worker reclaims the expired lease and does not redo finished steps."""
    counter_a, counter_b = tmp_path / "a.txt", tmp_path / "b.txt"
    deploy(services, wf(tmp_path, id="test.crash", steps=[
        {"kind": "tool", "id": "a", "tool": "test.count", "args": {"path": str(counter_a)}},
        {"kind": "tool", "id": "b", "tool": "test.count", "args": {"path": str(counter_b), "sleep_s": 30}},
        {"kind": "tool", "id": "c", "tool": "test.echo", "args": {"value": "done"}}], outputs={}, tests=[]))
    rid = services.runtime.enqueue("test.crash")
    script = tmp_path / "crash.py"
    script.write_text(CRASH_SCRIPT)
    proc = subprocess.Popen([sys.executable, str(script), rid, str(Path(__file__).resolve().parents[1])],
                            env={**os.environ, "FORGE_HOME": str(home)})
    for _ in range(100):  # wait until step b is running
        row = services.db.one("SELECT status FROM step_runs WHERE run_id=? AND step_path='b'", (rid,))
        if row and row["status"] == "running" and counter_b.exists():
            break
        time.sleep(0.2)
    proc.kill()   # hard crash, no cleanup
    proc.wait()
    assert services.db.one("SELECT status FROM runs WHERE id=?", (rid,))["status"] == "running"
    services.db.execute("UPDATE runs SET lease_expires=? WHERE id=?", (time.time() - 1, rid))   # lease runs out
    services.db.execute("UPDATE step_runs SET input_json=NULL WHERE run_id=?", (rid,))
    # make b fast for the resumed attempt
    from forge.workflows.ir import WorkflowIR
    claimed = services.runtime._claim()
    assert claimed and claimed["id"] == rid
    ver = services.db.one("SELECT ir_json FROM workflow_versions WHERE id=?", (claimed["workflow_version_id"],))
    ir = json.loads(ver["ir_json"])
    ir["steps"][1]["args"]["sleep_s"] = 0
    services.db.execute("UPDATE workflow_versions SET ir_json=? WHERE id=?", (json.dumps(ir), claimed["workflow_version_id"]))
    await services.runtime._execute(claimed)
    run = services.db.one("SELECT * FROM runs WHERE id=?", (rid,))
    assert run["status"] == "succeeded", run["error"]
    assert counter_a.read_text() == "1"      # finished step replayed from checkpoint, not re-executed
    assert counter_b.read_text() == "2"      # interrupted local step re-executed (at-least-once)


async def test_interrupted_external_step_waits_for_user(services, tmp_path):
    deploy(services, wf(tmp_path, id="test.ext", steps=[
        {"kind": "tool", "id": "s", "tool": "test.send", "args": {"to": "a@b.c", "body": "x"}}], outputs={}, tests=[]))
    rid = services.runtime.enqueue("test.ext")
    services.db.execute("INSERT INTO step_runs(run_id,step_path,kind,status,attempt,started_at) VALUES(?,?,?,?,1,?)",
                        (rid, "s", "tool", "running", time.time()))
    run = await services.runtime.execute_now(rid)
    assert run["status"] == "waiting_user"
