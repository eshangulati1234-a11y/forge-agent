"""Workflow versions, tests, dry-runs and the deploy gate.

Lifecycle: draft --(static check + tests + dry-run pass)--> approval requested --(user approves)--> deployed.
Deploying grants exactly the IR's `permissions` block; the approval subject carries the permission diff vs the
currently deployed version so widening is always visible to the approver.
"""
from __future__ import annotations

import json
from typing import Any

from forge.db import now, sha
from forge.errors import ErrorClass, ForgeError
from forge.workflows.ir import Issue, WorkflowIR, check_ir
from forge.workflows.template import evaluate


class WorkflowStore:
    def __init__(self, services):
        self.s = services
        self.db = services.db

    # ---- versions -------------------------------------------------------------------
    def create_version(self, ir: WorkflowIR, created_by: str, note: str = "", parent_version_id: int | None = None) -> dict:
        issues = check_ir(ir, self.s.registry)
        if issues:
            raise CompileError(issues)
        ir_json = ir.model_dump_json(by_alias=True)
        h = sha(json.loads(ir_json))
        with self.db.tx() as db:
            if not db.one("SELECT id FROM workflows WHERE id=?", (ir.id,)):
                db.execute("INSERT INTO workflows(id,description,created_at) VALUES(?,?,?)", (ir.id, ir.description, now()))
            same = db.one("SELECT * FROM workflow_versions WHERE workflow_id=? AND ir_hash=? AND status IN ('draft','approved','deployed')", (ir.id, h))
            if same:
                return same
            v = (db.one("SELECT MAX(version) AS v FROM workflow_versions WHERE workflow_id=?", (ir.id,))["v"] or 0) + 1
            cur = db.execute("INSERT INTO workflow_versions(workflow_id,version,ir_json,ir_hash,status,created_by,parent_version_id,note,created_at)"
                             " VALUES(?,?,?,?,?,?,?,?,?)", (ir.id, v, ir_json, h, "draft", created_by, parent_version_id, note, now()))
            vid = cur.lastrowid
        self.db.audit(created_by, "workflow.version_created", {"workflow_id": ir.id, "version": v, "ir_hash": h})
        return self.db.one("SELECT * FROM workflow_versions WHERE id=?", (vid,))

    def ir(self, version_id: int) -> WorkflowIR:
        return WorkflowIR.model_validate_json(self.db.one("SELECT ir_json FROM workflow_versions WHERE id=?", (version_id,))["ir_json"])

    def deployed(self, workflow_id: str) -> dict | None:
        return self.db.one("SELECT * FROM workflow_versions WHERE workflow_id=? AND status='deployed' ORDER BY version DESC LIMIT 1", (workflow_id,))

    def list(self) -> list[dict]:
        return self.db.all(
            "SELECT w.id, w.description, (SELECT MAX(version) FROM workflow_versions v WHERE v.workflow_id=w.id) AS latest,"
            " (SELECT version FROM workflow_versions v WHERE v.workflow_id=w.id AND v.status='deployed' ORDER BY version DESC LIMIT 1) AS deployed,"
            " (SELECT status FROM runs r WHERE r.workflow_id=w.id ORDER BY created_at DESC LIMIT 1) AS last_run_status"
            " FROM workflows w ORDER BY w.id")

    def versions(self, workflow_id: str) -> list[dict]:
        return self.db.all("SELECT id,workflow_id,version,status,created_by,note,created_at,ir_hash FROM workflow_versions WHERE workflow_id=? ORDER BY version DESC",
                           (workflow_id,))

    # ---- verification ------------------------------------------------------------------
    async def run_tests(self, version_id: int) -> list[dict]:
        ir = self.ir(version_id)
        results = []
        for t in ir.tests:
            rid = self.s.runtime.enqueue(ir.id, trigger="test", version_id=version_id, dry_run=True,
                                         inputs={**t.inputs, "__mocks__": t.mocks}, priority=1)
            run = await self.s.runtime.execute_now(rid)
            env = self._result_env(run)
            failed = []
            for e in t.expect:
                try:
                    if not evaluate(e, env):
                        failed.append(e)
                except Exception as ex:
                    failed.append(f"{e}  ({type(ex).__name__}: {ex})")
            results.append({"test": t.name, "run_id": rid, "status": run["status"], "passed": run["status"] == "succeeded" and not failed,
                            "failed_expectations": failed, "error": run["error"] if run["status"] != "succeeded" else None})
        return results

    async def dry_run(self, version_id: int, inputs: dict | None = None) -> dict:
        ir = self.ir(version_id)
        rid = self.s.runtime.enqueue(ir.id, trigger="dry_run", version_id=version_id, dry_run=True, inputs=inputs or {}, priority=1)
        run = await self.s.runtime.execute_now(rid)
        return {"run_id": rid, "status": run["status"], "error": run["error"] if run["status"] != "succeeded" else None,
                "steps": self.db.all("SELECT step_path,status,error_class,error FROM step_runs WHERE run_id=? ORDER BY started_at", (rid,))}

    def _result_env(self, run: dict) -> dict:
        steps = {}
        for r in self.db.all("SELECT step_path,output_json FROM step_runs WHERE run_id=?", (run["id"],)):
            if "." not in r["step_path"] and "[" not in r["step_path"]:
                steps[r["step_path"]] = json.loads(r["output_json"]) if r["output_json"] else None
        outputs = json.loads(run["outputs_json"]) if run.get("outputs_json") else {}
        return {"steps": steps, "outputs": outputs, "status": run["status"]}

    # ---- deploy gate -------------------------------------------------------------------
    def request_deploy(self, version_id: int, requested_by: str, verification: dict | None = None) -> dict:
        v = self.db.one("SELECT * FROM workflow_versions WHERE id=?", (version_id,))
        ir = WorkflowIR.model_validate_json(v["ir_json"])
        cur = self.deployed(ir.id)
        old_perms = WorkflowIR.model_validate_json(cur["ir_json"]).permissions.model_dump() if cur else None
        new_perms = ir.permissions.model_dump()
        diff = _perm_diff(old_perms, new_perms)
        subject = {"type": "deploy", "workflow_id": ir.id, "version_id": version_id, "version": v["version"], "ir_hash": v["ir_hash"],
                   "permissions": new_perms, "permission_diff": diff, "verification": verification or {},
                   "triggers": [t.model_dump(exclude_none=True) for t in ir.triggers]}
        widen = " ⚠ widens permissions" if any(diff.get("added", {}).values()) else ""
        return self.s.approvals.request("deploy", f"deploy {ir.id} v{v['version']}{widen}", subject, requested_by=requested_by)

    def activate(self, approval: dict) -> dict:
        """Called when a deploy approval is approved (signature already verified by caller)."""
        subj = json.loads(approval["subject_json"])
        v = self.db.one("SELECT * FROM workflow_versions WHERE id=?", (subj["version_id"],))
        if v["ir_hash"] != subj["ir_hash"]:
            raise ForgeError("workflow version changed after approval was requested", ErrorClass.PERMISSION_DENIED)
        ir = WorkflowIR.model_validate_json(v["ir_json"])
        with self.db.tx() as db:
            db.execute("UPDATE workflow_versions SET status='retired' WHERE workflow_id=? AND status='deployed'", (ir.id,))
            db.execute("UPDATE workflow_versions SET status='deployed' WHERE id=?", (v["id"],))
            db.execute("INSERT INTO grants(workflow_id,permissions_json,approval_id,granted_at) VALUES(?,?,?,?)"
                       " ON CONFLICT(workflow_id) DO UPDATE SET permissions_json=excluded.permissions_json, approval_id=excluded.approval_id,"
                       " granted_at=excluded.granted_at", (ir.id, ir.permissions.model_dump_json(), approval["id"], now()))
        self.s.scheduler.sync_workflow(ir)
        self.db.audit(approval.get("decided_by") or "user", "workflow.deployed", {"workflow_id": ir.id, "version": v["version"]})
        self.s.events.emit("workflow.deployed", workflow_id=ir.id, version=v["version"])
        return {"workflow_id": ir.id, "version": v["version"]}

    def rollback(self, workflow_id: str, to_version: int | None = None) -> dict:
        """Re-deploy a previous version. Rolling back can only narrow or keep permissions it previously had."""
        cur = self.deployed(workflow_id)
        q = "SELECT * FROM workflow_versions WHERE workflow_id=? AND status='retired'" + (" AND version=?" if to_version else "") + " ORDER BY version DESC LIMIT 1"
        prev = self.db.one(q, (workflow_id, to_version) if to_version else (workflow_id,))
        if not prev:
            raise ForgeError("no previous deployed version to roll back to", ErrorClass.INVALID_INPUT)
        with self.db.tx() as db:
            if cur:
                db.execute("UPDATE workflow_versions SET status='rejected', note=note||' [rolled back]' WHERE id=?", (cur["id"],))
            db.execute("UPDATE workflow_versions SET status='deployed' WHERE id=?", (prev["id"],))
        ir = WorkflowIR.model_validate_json(prev["ir_json"])
        self.db.execute("UPDATE grants SET permissions_json=? WHERE workflow_id=?", (ir.permissions.model_dump_json(), workflow_id))
        self.s.scheduler.sync_workflow(ir)
        self.db.audit("user", "workflow.rolled_back", {"workflow_id": workflow_id, "to": prev["version"]})
        return {"workflow_id": workflow_id, "version": prev["version"]}

    def handle_approval(self, approval: dict) -> Any:
        """Route a decided approval to whatever was waiting on it."""
        subj = json.loads(approval["subject_json"])
        if approval["status"] == "approved" and subj.get("type") == "deploy":
            if not self.s.approvals.verify(approval):
                raise ForgeError("approval signature invalid", ErrorClass.PERMISSION_DENIED)
            return self.activate(approval)
        if approval["status"] == "approved" and subj.get("type") == "register_tool":
            from pathlib import Path

            from forge.tools.toolgen import register_generated
            vdir = Path(subj["path"])
            manifest = json.loads((vdir / "manifest.json").read_text())
            if sha((vdir / "tool.py").read_text(encoding="utf-8")) != subj["code_sha256"] or not self.s.approvals.verify(approval):
                raise ForgeError("generated tool changed on disk after approval was requested", ErrorClass.PERMISSION_DENIED)
            register_generated(self.s, manifest, vdir / "tool.py")
            self.db.audit("user", "tool.registered", {"name": subj["name"], "code_sha256": subj["code_sha256"]})
            return {"registered": subj["name"]}
        if approval["status"] == "denied" and subj.get("type") == "deploy":
            self.db.execute("UPDATE workflow_versions SET status='rejected' WHERE id=? AND status='draft'", (subj["version_id"],))
        self.s.runtime.on_approval_decided(approval["id"])
        return None


class CompileError(ForgeError):
    error_class = ErrorClass.INVALID_INPUT

    def __init__(self, issues: list[Issue]):
        super().__init__("; ".join(f"{i.path}: {i.message}" for i in issues))
        self.issues = issues


def _perm_diff(old: dict | None, new: dict) -> dict:
    if old is None:
        return {"added": {k: v for k, v in new.items() if v}, "removed": {}}
    added, removed = {}, {}
    for k, v in new.items():
        if isinstance(v, list):
            a, r = sorted(set(v) - set(old.get(k, []))), sorted(set(old.get(k, [])) - set(v))
            if a:
                added[k] = a
            if r:
                removed[k] = r
        elif v != old.get(k):
            (added if v else removed)[k] = True
    return {"added": added, "removed": removed}
