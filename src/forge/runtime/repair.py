"""Self-repair: failed run -> diagnosis -> candidate IR patch -> tests + dry-run -> deploy approval.

Hard rules:
  * Only REPAIRABLE classes (interface_changed, defect, invalid_input). Transient errors are retried, not repaired;
    permission/credential/approval failures are reported to the user, never "fixed".
  * The patched IR must keep `permissions` byte-identical. A repair that needs more access is rejected and the
    user is told what access is missing; widening is only possible through a fresh, explicit deploy approval.
  * Repairs always go through the deploy approval gate. A repaired version that fails within its first runs is
    rolled back automatically to its parent.
  * At most one proposal per workflow per hour.
"""
from __future__ import annotations

import json
import logging

from pydantic import ValidationError

from forge.db import now
from forge.errors import REPAIRABLE, ErrorClass
from forge.workflows.ir import WorkflowIR, check_ir
from forge.workflows.registry import CompileError

log = logging.getLogger("forge.repair")
COOLDOWN_S = 3600
REGRESSION_WINDOW = 3


class RepairEngine:
    def __init__(self, services):
        self.s = services
        self.db = services.db

    def diagnose(self, run_id: str) -> dict:
        run = self.db.one("SELECT * FROM runs WHERE id=?", (run_id,))
        failed = self.db.one("SELECT * FROM step_runs WHERE run_id=? AND status='failed' ORDER BY finished_at DESC LIMIT 1", (run_id,))
        last_ok = self.db.one("SELECT id FROM runs WHERE workflow_id=? AND status='succeeded' AND trigger NOT IN ('test','dry_run') "
                              "ORDER BY finished_at DESC LIMIT 1", (run["workflow_id"],))
        prev_out = None
        if last_ok and failed:
            r = self.db.one("SELECT output_json FROM step_runs WHERE run_id=? AND step_path=?", (last_ok["id"], failed["step_path"]))
            prev_out = r["output_json"][:3000] if r and r["output_json"] else None
        calls = self.db.all("SELECT tool,args_json,status,error,error_class FROM tool_calls WHERE run_id=? ORDER BY created_at DESC LIMIT 5", (run_id,))
        return {"run_id": run_id, "workflow_id": run["workflow_id"], "version_id": run["workflow_version_id"],
                "error": run["error"], "error_class": run["error_class"],
                "failed_step": failed["step_path"] if failed else None, "step_error": failed["error"] if failed else None,
                "previous_success_output": prev_out, "recent_tool_calls": calls}

    async def propose(self, run_id: str) -> dict | None:
        d = self.diagnose(run_id)
        try:
            cls = ErrorClass(d["error_class"])
        except ValueError:
            return None
        if cls not in REPAIRABLE:
            return None
        if self.check_regression(d):
            return {"rolled_back": True}
        key = f"repair_last:{d['workflow_id']}"
        if now() - float(self.db.get_kv(key, "0")) < COOLDOWN_S:
            return None
        self.db.set_kv(key, str(now()))
        ir = self.s.workflows.ir(d["version_id"])
        self.s.events.emit("repair.started", run_id=run_id, workflow_id=ir.id, failed_step=d["failed_step"])
        from forge.workflows.compiler import IR_GUIDE
        prompt = (f"A deployed workflow failed.\nDiagnosis:\n{json.dumps(d, default=str)[:6000]}\n\nCurrent workflow JSON:\n"
                  f"{ir.model_dump_json(by_alias=True)}\n\nTools:\n" +
                  "\n".join(f"- {t.name}: {json.dumps(t.json_schema().get('properties', {}))[:300]}" for t in self.s.registry.all()) +
                  "\n\nReturn JSON {\"diagnosis\": str, \"workflow\": <complete corrected workflow>}. Do NOT change \"permissions\". "
                  "If the fix needs more permissions, return {\"diagnosis\": str, \"needs_permission\": str} instead.")
        out = await self.s.router.generate("coding", [{"role": "system", "content": IR_GUIDE}, {"role": "user", "content": prompt}],
                                           schema="json", temperature=0.1, max_tokens=4000)  # type: ignore[arg-type]
        try:
            data = out.json()
        except json.JSONDecodeError:
            return self._give_up(ir.id, run_id, "repair model returned invalid JSON")
        if data.get("needs_permission"):
            self.s.notify(f"Repair for {ir.id} needs more access", data["needs_permission"])
            return self._give_up(ir.id, run_id, f"needs permission: {data['needs_permission']}")
        try:
            new = WorkflowIR.model_validate(data.get("workflow") or {})
        except ValidationError as e:
            return self._give_up(ir.id, run_id, f"invalid patched IR: {e.errors()[:3]}")
        if new.permissions.model_dump() != ir.permissions.model_dump() or new.id != ir.id:
            return self._give_up(ir.id, run_id, "repair attempted to change permissions or id; rejected")
        if check_ir(new, self.s.registry):
            return self._give_up(ir.id, run_id, "patched IR has compile errors")
        try:
            v = self.s.workflows.create_version(new, created_by="repair", note=f"repair of {run_id}: {data.get('diagnosis', '')[:300]}",
                                                parent_version_id=d["version_id"])
        except CompileError as e:
            return self._give_up(ir.id, run_id, str(e))
        tests = await self.s.workflows.run_tests(v["id"])
        dry = await self.s.workflows.dry_run(v["id"])
        if not all(t["passed"] for t in tests) or dry["status"] not in ("succeeded", "waiting_user"):
            return self._give_up(ir.id, run_id, f"candidate failed verification (dry run {dry['status']})")
        ap = self.s.workflows.request_deploy(v["id"], "repair", {"diagnosis": data.get("diagnosis"), "tests": tests,
                                                                  "dry_run_status": dry["status"], "repairs_run": run_id})
        self.s.notify(f"Forge proposes a fix for {ir.id}", data.get("diagnosis", "")[:200])
        self.s.events.emit("repair.proposed", workflow_id=ir.id, version=v["version"], approval_id=ap["id"])
        return {"version_id": v["id"], "approval_id": ap["id"], "diagnosis": data.get("diagnosis")}

    def _give_up(self, workflow_id: str, run_id: str, reason: str) -> dict:
        log.info("repair of %s abandoned: %s", run_id, reason)
        self.db.audit("repair", "repair.abandoned", {"workflow_id": workflow_id, "run_id": run_id, "reason": reason})
        self.s.events.emit("repair.abandoned", workflow_id=workflow_id, run_id=run_id, reason=reason)
        return {"abandoned": reason}

    def check_regression(self, d: dict) -> bool:
        """If the failing version is a repair and it fails early in its life, roll back to its parent."""
        v = self.db.one("SELECT * FROM workflow_versions WHERE id=?", (d["version_id"],))
        if not v or v["created_by"] != "repair" or v["status"] != "deployed" or not v["parent_version_id"]:
            return False
        n = self.db.one("SELECT COUNT(*) AS n FROM runs WHERE workflow_version_id=? AND trigger NOT IN ('test','dry_run')", (v["id"],))["n"]
        if n > REGRESSION_WINDOW:
            return False
        parent = self.db.one("SELECT version FROM workflow_versions WHERE id=?", (v["parent_version_id"],))
        self.s.workflows.rollback(d["workflow_id"], parent["version"])
        self.s.notify(f"Rolled back {d['workflow_id']}", f"repaired v{v['version']} regressed; restored v{parent['version']}")
        return True
