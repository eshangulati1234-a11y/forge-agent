"""Durable workflow runtime.

* Runs live in SQLite. Workers claim them with a lease and heartbeat; a crashed daemon's runs are reclaimed
  when the lease expires, and execution resumes from per-step checkpoints (succeeded steps are replayed
  from their stored output, never re-executed).
* At-least-once safety: a step found in `running` state after a crash is re-executed only if it has no
  external side effect or carries an idempotency key; otherwise the run stops in `waiting_user` so a human
  can confirm whether the action already happened.
* Retries only for transient/resource errors, with exponential backoff.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any

from forge.db import canonical, new_id, now
from forge.errors import (REPAIRABLE, RETRYABLE, EmergencyStop, ErrorClass, ForgeError, InvalidInput, NeedsApproval,
                          NeedsUser, PermissionDenied, classify)
from forge.tools.invoke import invoke_tool
from forge.tools.registry import SideEffect, ToolContext
from forge.workflows.ir import (ApprovalStep, BranchStep, DecideStep, ForeachStep, LlmStep, ParallelStep, RetryPolicy,
                                SetStep, ToolStep, WorkflowIR)
from forge.workflows.template import evaluate, render

log = logging.getLogger("forge.runtime")
WORKER_ID = f"{os.getpid()}"


class RunCancelled(ForgeError):
    error_class = ErrorClass.CANCELLED


class _Pause(Exception):
    """Internal: stop executing this run without failing it."""
    def __init__(self, status: str, reason: str, waiting_on: str | None = None):
        self.status, self.reason, self.waiting_on = status, reason, waiting_on


class Runtime:
    def __init__(self, services):
        self.s = services
        self.db = services.db
        self._tasks: dict[str, asyncio.Task] = {}
        self._wake = asyncio.Event()
        self._stopping = False

    # ------------------------------------------------------------------ enqueue / control
    def enqueue(self, workflow_id: str, *, trigger: str = "manual", inputs: dict | None = None, version_id: int | None = None,
                dedupe_key: str | None = None, priority: int = 5, dry_run: bool = False, not_before: float = 0) -> str | None:
        if version_id is None:
            v = self.db.one("SELECT id FROM workflow_versions WHERE workflow_id=? AND status='deployed' ORDER BY version DESC LIMIT 1",
                            (workflow_id,))
            if not v:
                raise ForgeError(f"workflow '{workflow_id}' has no deployed version", ErrorClass.INVALID_INPUT)
            version_id = v["id"]
        run_id = new_id("run_")
        try:
            self.db.execute("INSERT INTO runs(id,workflow_version_id,workflow_id,trigger,status,priority,inputs_json,dry_run,dedupe_key,not_before,created_at)"
                            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                            (run_id, version_id, workflow_id, trigger, "queued", priority, canonical(inputs or {}), int(dry_run),
                             dedupe_key, not_before, now()))
        except Exception as e:  # dedupe hit
            if "UNIQUE" in str(e) and dedupe_key:
                return None
            raise
        self.db.audit(trigger, "run.enqueued", {"run_id": run_id, "workflow_id": workflow_id, "version_id": version_id})
        self.s.events.emit("run.queued", run_id=run_id, workflow_id=workflow_id, trigger=trigger)
        self._wake.set()
        return run_id

    def cancel(self, run_id: str) -> None:
        self.db.execute("UPDATE runs SET status='cancelled', finished_at=? WHERE id=? AND status IN ('queued','waiting_approval','waiting_user','running')",
                        (now(), run_id))
        t = self._tasks.get(run_id)
        if t:
            t.cancel()
        self.s.events.emit("run.cancelled", run_id=run_id)

    def on_approval_decided(self, approval_id: str) -> None:
        rows = self.db.all("SELECT id FROM runs WHERE status='waiting_approval' AND error=?", (approval_id,))
        for r in rows:
            self.db.execute("UPDATE runs SET status='queued', error=NULL, error_class=NULL, lease_owner=NULL WHERE id=?", (r["id"],))
            self.s.events.emit("run.resumed", run_id=r["id"], approval_id=approval_id)
        if rows:
            self._wake.set()

    def resume_user(self, run_id: str, confirm_step_done: bool) -> None:
        """Human resolves a `waiting_user` run. If they confirm the in-doubt external step happened, mark it done."""
        if confirm_step_done:
            self.db.execute("UPDATE step_runs SET status='succeeded', output_json=?, finished_at=? WHERE run_id=? AND status='running'",
                            (canonical({"confirmed_by_user": True}), now(), run_id))
        else:
            self.db.execute("DELETE FROM step_runs WHERE run_id=? AND status IN ('running','failed')", (run_id,))
        self.db.execute("UPDATE runs SET status='queued', error=NULL, error_class=NULL WHERE id=? AND status IN ('waiting_user','failed')", (run_id,))
        self._wake.set()

    # ------------------------------------------------------------------ worker loop
    async def serve(self) -> None:
        log.info("runtime worker %s started", WORKER_ID)
        while not self._stopping:
            try:
                await self._fill_slots()
            except Exception:
                log.exception("worker loop error")
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.s.cfg.daemon.scheduler_tick_seconds)
            except asyncio.TimeoutError:
                pass

    async def stop(self) -> None:
        self._stopping = True
        self._wake.set()
        for t in list(self._tasks.values()):
            t.cancel()

    async def _fill_slots(self) -> None:
        if self.s.policy.stopped():
            return
        while len(self._tasks) < self.s.cfg.daemon.max_concurrent_runs:
            run = self._claim()
            if not run:
                return
            t = asyncio.create_task(self._execute(run))
            self._tasks[run["id"]] = t
            t.add_done_callback(lambda _t, rid=run["id"]: self._tasks.pop(rid, None))

    def _claim(self) -> dict | None:
        lease = self.s.cfg.daemon.lease_seconds
        with self.db.tx() as db:
            row = db.one("SELECT * FROM runs WHERE (status='queued' AND not_before<=?) OR (status='running' AND lease_expires<?)"
                         " ORDER BY priority, created_at LIMIT 1", (now(), now()))
            if not row:
                return None
            if row["status"] == "running":
                log.warning("reclaiming run %s from expired lease (owner %s)", row["id"], row["lease_owner"])
            db.execute("UPDATE runs SET status='running', lease_owner=?, lease_expires=?, started_at=COALESCE(started_at,?), attempt=attempt+1 WHERE id=?",
                       (WORKER_ID, now() + lease, now(), row["id"]))
        return row

    async def _heartbeat(self, run_id: str) -> None:
        lease = self.s.cfg.daemon.lease_seconds
        while True:
            await asyncio.sleep(lease / 3)
            self.db.execute("UPDATE runs SET lease_expires=? WHERE id=? AND status='running'", (now() + lease, run_id))

    # ------------------------------------------------------------------ execution
    async def execute_now(self, run_id: str) -> dict:
        """Synchronous execution (tests, dry-runs, CLI). Claims the run directly."""
        row = self.db.one("SELECT * FROM runs WHERE id=?", (run_id,))
        self.db.execute("UPDATE runs SET status='running', lease_owner=?, lease_expires=?, started_at=COALESCE(started_at,?), attempt=attempt+1 WHERE id=?",
                        (WORKER_ID, now() + 3600, now(), run_id))
        await self._execute(row)
        return self.db.one("SELECT * FROM runs WHERE id=?", (run_id,))

    async def _execute(self, run: dict) -> None:
        run_id = run["id"]
        hb = asyncio.create_task(self._heartbeat(run_id))
        ver = self.db.one("SELECT * FROM workflow_versions WHERE id=?", (run["workflow_version_id"],))
        ir = WorkflowIR.model_validate_json(ver["ir_json"])
        inputs = {k: spec.default for k, spec in ir.inputs.items() if spec.default is not None}
        inputs.update(json.loads(run["inputs_json"] or "{}"))
        ctx = {"inputs": inputs, "steps": {}, "run": {"id": run_id, "trigger": run["trigger"], "workflow_id": ir.id}}
        state = _RunState(run_id=run_id, ir=ir, version_id=ver["id"], dry_run=bool(run["dry_run"]),
                          mocks=json.loads(run["inputs_json"] or "{}").get("__mocks__", {}) if run["trigger"] == "test" else {})
        self.s.events.emit("run.started", run_id=run_id, workflow_id=ir.id, version=ver["version"])
        try:
            missing = [k for k, sp in ir.inputs.items() if sp.required and k not in inputs]
            if missing:
                raise InvalidInput(f"missing required inputs: {missing}")
            await self._run_steps(ir.steps, ctx, "", state)
            outputs = {k: render(v, ctx) for k, v in ir.outputs.items()}
            self._finish(run_id, "succeeded", outputs=outputs)
        except _Pause as p:
            # waiting_approval: error holds the approval id so on_approval_decided can find and requeue the run
            err, cls = (p.waiting_on, "needs_approval") if p.status == "waiting_approval" else (p.reason[:2000], "needs_user")
            self.db.execute("UPDATE runs SET status=?, error=?, error_class=?, lease_owner=NULL WHERE id=?", (p.status, err, cls, run_id))
            self.s.events.emit(f"run.{p.status}", run_id=run_id, reason=p.reason, approval_id=p.waiting_on)
            if p.status == "waiting_user":
                self.s.notify(f"Forge needs you: {ir.name}", p.reason)
        except (asyncio.CancelledError, RunCancelled):
            self._finish(run_id, "cancelled", error="cancelled")
        except EmergencyStop as e:
            self.db.execute("UPDATE runs SET status='queued', lease_owner=NULL, error=? WHERE id=?", (str(e), run_id))
            self.s.events.emit("run.parked", run_id=run_id, reason=str(e))
        except Exception as e:
            cls = classify(e)
            log.warning("run %s failed (%s): %s", run_id, cls, e)
            self._finish(run_id, "failed", error=f"{type(e).__name__}: {e}"[:2000], error_class=str(cls))
            self.s.notify(f"Forge workflow failed: {ir.name}", f"{cls}: {e}"[:200])
            if cls in REPAIRABLE and self.s.repair and not state.dry_run and run["trigger"] not in ("test", "dry_run", "repair_test"):
                asyncio.create_task(self.s.repair.propose(run_id))
        finally:
            hb.cancel()

    def _finish(self, run_id: str, status: str, *, outputs: dict | None = None, error: str | None = None, error_class: str | None = None) -> None:
        self.db.execute("UPDATE runs SET status=?, finished_at=?, outputs_json=?, error=?, error_class=?, lease_owner=NULL WHERE id=?",
                        (status, now(), canonical(outputs) if outputs is not None else None, error, error_class, run_id))
        self.db.audit("runtime", f"run.{status}", {"run_id": run_id, "error": error})
        self.s.events.emit(f"run.{status}", run_id=run_id, error=error, outputs=outputs)

    async def _run_steps(self, steps: list, ctx: dict, prefix: str, st: "_RunState") -> None:
        for step in steps:
            await self._run_step(step, ctx, prefix + step.id, st)

    async def _run_step(self, step, ctx: dict, path: str, st: "_RunState") -> None:
        cur = self.db.one("SELECT status FROM runs WHERE id=?", (st.run_id,))
        if cur and cur["status"] == "cancelled":
            raise RunCancelled("run cancelled")
        if self.s.policy.stopped():
            raise EmergencyStop("emergency stop active")

        cp = self.db.one("SELECT * FROM step_runs WHERE run_id=? AND step_path=?", (st.run_id, path))
        if cp and cp["status"] in ("succeeded", "skipped"):
            ctx["steps"][step.id] = json.loads(cp["output_json"]) if cp["output_json"] else None
            return
        if cp and cp["status"] == "running" and isinstance(step, ToolStep):
            spec = self.s.registry.get(step.tool)
            if spec and spec.side_effect == SideEffect.EXTERNAL and not step.idempotency_key:
                raise _Pause("waiting_user", f"step '{path}' ({step.tool}) was interrupted mid-flight; confirm whether it completed")

        if step.when is not None and not evaluate(step.when, ctx):
            self._checkpoint(st.run_id, path, step.kind, "skipped", output=None)
            ctx["steps"][step.id] = None
            self.s.events.emit("step.skipped", run_id=st.run_id, step=path)
            return

        policy = step.retry or RetryPolicy(max_attempts=1)
        attempt = 0
        while True:
            attempt += 1
            self._checkpoint(st.run_id, path, step.kind, "running", attempt=attempt)
            self.s.events.emit("step.start", run_id=st.run_id, step=path, step_kind=step.kind, attempt=attempt)
            t0 = time.time()
            try:
                out = await self._dispatch(step, ctx, path, st)
                ctx["steps"][step.id] = out
                self._checkpoint(st.run_id, path, step.kind, "succeeded", output=out, attempt=attempt)
                self.s.events.emit("step.end", run_id=st.run_id, step=path, ms=int((time.time() - t0) * 1000))
                return
            except NeedsApproval as e:
                self._checkpoint(st.run_id, path, step.kind, "waiting_approval", error=str(e), attempt=attempt)
                raise _Pause("waiting_approval", str(e), e.approval_id)
            except NeedsUser as e:
                self._checkpoint(st.run_id, path, step.kind, "failed", error=str(e), error_class="needs_user", attempt=attempt)
                raise _Pause("waiting_user", str(e))
            except (_Pause, RunCancelled, EmergencyStop, asyncio.CancelledError):
                raise
            except Exception as e:
                cls = classify(e)
                self._checkpoint(st.run_id, path, step.kind, "failed", error=f"{type(e).__name__}: {e}"[:4000],
                                 error_class=str(cls), attempt=attempt)
                self.s.events.emit("step.error", run_id=st.run_id, step=path, error=str(e)[:300], error_class=str(cls))
                if cls in RETRYABLE and attempt < policy.max_attempts and self._retry_safe(step):
                    delay = min(policy.max_backoff_s, policy.backoff_s * policy.factor ** (attempt - 1))
                    await asyncio.sleep(delay)
                    continue
                if step.continue_on_error and cls not in (ErrorClass.PERMISSION_DENIED,):
                    ctx["steps"][step.id] = {"error": str(e), "error_class": str(cls)}
                    self._checkpoint(st.run_id, path, step.kind, "succeeded", output=ctx["steps"][step.id], attempt=attempt)
                    return
                raise

    def _retry_safe(self, step) -> bool:
        if isinstance(step, ToolStep):
            spec = self.s.registry.get(step.tool)
            if spec and spec.side_effect == SideEffect.EXTERNAL:
                return bool(step.idempotency_key)
        return True

    def _checkpoint(self, run_id, path, kind, status, *, output=None, error=None, error_class=None, attempt=1) -> None:
        self.db.execute(
            "INSERT INTO step_runs(run_id,step_path,kind,status,attempt,output_json,error,error_class,started_at,finished_at) VALUES(?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(run_id,step_path) DO UPDATE SET status=excluded.status, attempt=excluded.attempt, output_json=excluded.output_json,"
            " error=excluded.error, error_class=excluded.error_class, finished_at=excluded.finished_at",
            (run_id, path, kind, status, attempt, canonical(output) if output is not None else None, error, error_class, now(),
             None if status == "running" else now()))

    # ------------------------------------------------------------------ step kinds
    async def _dispatch(self, step, ctx: dict, path: str, st: "_RunState") -> Any:
        if isinstance(step, ToolStep):
            args = render(step.args, ctx)
            if step.tool in st.mocks:
                return st.mocks[step.tool]
            tctx = ToolContext(self.s, run_id=st.run_id, workflow_id=st.ir.id, step_path=path, dry_run=st.dry_run,
                               extra={"permissions": st.ir.permissions})
            scope = {"run_id": st.run_id, "workflow_version_id": st.version_id, "step": path}
            return await invoke_tool(self.s, step.tool, args, st.ir.permissions, tctx, interactive=False,
                                     timeout_s=step.timeout_s, scope=scope)
        if isinstance(step, SetStep):
            return render(step.values, ctx)
        if isinstance(step, LlmStep):
            if "llm" in st.mocks:
                return st.mocks["llm"]
            msgs = ([{"role": "system", "content": render(step.system, ctx)}] if step.system else [])
            msgs.append({"role": "user", "content": render(step.prompt, ctx)})
            res = await self.s.router.generate(step.capability, msgs, schema=step.output_schema, complexity=step.complexity,
                                               max_tokens=step.max_tokens, temperature=0.1)
            if step.output_schema:
                import jsonschema
                data = res.json()
                jsonschema.validate(data, step.output_schema)
                return data
            return {"text": res.content}
        if isinstance(step, DecideStep):
            return await self.s.decisions.decide(step, ctx, mocks=st.mocks)
        if isinstance(step, ApprovalStep):
            title = render(step.title, ctx)
            subject = {"type": "workflow_step", "run_id": st.run_id, "step": path, "title": title, "details": render(step.details, ctx)}
            status, row = self.s.approvals.status_for(subject)
            if status in ("approved", "denied"):
                return {"approved": status == "approved", "approval_id": row["id"], "reason": row.get("reason")}
            if st.dry_run:
                return {"approved": True, "dry_run": True}
            ap = self.s.approvals.request("action", f"[{st.ir.name}] {title}", subject, requested_by=st.run_id, ttl=step.ttl_s)
            self.s.notify("Forge approval needed", title)
            raise NeedsApproval(title, ap["id"])
        if isinstance(step, BranchStep):
            taken = bool(evaluate(step.condition, ctx))
            await self._run_steps(step.then if taken else step.otherwise, ctx, f"{path}.{'then' if taken else 'else'}.", st)
            return {"taken": "then" if taken else "else"}
        if isinstance(step, ForeachStep):
            items = evaluate(step.items, ctx) or []
            if not isinstance(items, list):
                raise InvalidInput(f"foreach '{path}' items must be a list, got {type(items).__name__}")
            items = items[: step.max_items]
            sem = asyncio.Semaphore(step.max_parallel)
            results: list[Any] = [None] * len(items)

            async def one(i, item):
                async with sem:
                    sub = {**ctx, step.as_: item, "loop": {"index": i}, "steps": dict(ctx["steps"])}
                    await self._run_steps(step.steps, sub, f"{path}[{i}].", st)
                    results[i] = {s.id: sub["steps"].get(s.id) for s in step.steps}
            await asyncio.gather(*(one(i, it) for i, it in enumerate(items)))
            return results
        if isinstance(step, ParallelStep):
            outs: dict[str, Any] = {}

            async def branch(name, steps):
                sub = {**ctx, "steps": dict(ctx["steps"])}
                await self._run_steps(steps, sub, f"{path}.{name}.", st)
                outs[name] = {s.id: sub["steps"].get(s.id) for s in steps}
            await asyncio.gather(*(branch(n, b) for n, b in step.branches.items()))
            return outs
        raise InvalidInput(f"unknown step kind {step.kind}")


class _RunState:
    def __init__(self, run_id: str, ir: WorkflowIR, version_id: int, dry_run: bool, mocks: dict):
        self.run_id, self.ir, self.version_id, self.dry_run, self.mocks = run_id, ir, version_id, dry_run, mocks
