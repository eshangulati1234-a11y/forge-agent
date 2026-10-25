"""Automation compiler: natural language -> typed workflow IR -> static check -> tests -> dry-run -> deploy approval.

The model writes IR JSON (Ollama JSON mode guarantees it parses); pydantic + check_ir produce structured compile
errors that are fed back for up to MAX_REPAIRS rounds. The model never chooses its own permissions silently:
whatever it declares is shown to the user as a permission diff in the deploy approval.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from forge.workflows.ir import WorkflowIR, check_ir
from forge.workflows.registry import CompileError

log = logging.getLogger("forge.compiler")
MAX_REPAIRS = 3

IR_GUIDE = """\
Return ONE JSON object describing a workflow. Schema (informal):
{
 "id": "dotted.lower_snake", "name": str, "description": str,
 "triggers": [{"type": "manual"} | {"type":"cron","cron":"0 8 * * *"} | {"type":"interval","every_s":3600}
              | {"type":"file","path":"C:/dir","pattern":"*.pdf"} | {"type":"webhook"}],
 "inputs": {"name": {"type":"string|number|integer|boolean|array|object","default":..., "required":bool}},
 "permissions": {"tools":[exact tool names used], "fs_read":[dirs], "fs_write":[dirs], "net":[hostnames, may use *.domain],
                 "desktop":bool, "browser":bool},
 "steps": [ STEP, ... ],
 "outputs": {"name": "{{ expression }}"},
 "tests": [{"name": str, "inputs": {}, "mocks": {"tool.name": <canned output>, "llm": <canned llm output>, "decide": <canned decision>},
            "expect": ["jinja expression"]}],
 "missing_tools": [{"name": "...", "description": "what capability is missing"}]   // only if no tool can do a required step
}
STEP kinds (every step has a unique lower_snake "id"; optional "when": "<jinja expr>", "retry": {"max_attempts":3}):
 {"kind":"tool","id":..,"tool":"<tool name>","args":{...}}
 {"kind":"llm","id":..,"prompt":"...{{ steps.x.text }}...","schema":{JSON schema of the output object},"capability":"extraction|reasoning|coding"}
 {"kind":"decide","id":..,"mode":"choice|score|yes_no","state":"text describing the situation","question":"...",
    "options":{"label":"description"} (choice) , "levels":["worst",...,"best"] (score)}
 {"kind":"set","id":..,"values":{"k":"{{ expr }}"}}
 {"kind":"approval","id":..,"title":"what the user must approve","details":"..."}
 {"kind":"branch","id":..,"condition":"<jinja expr>","then":[steps],"else":[steps]}
 {"kind":"foreach","id":..,"items":"<jinja expr giving a list>","as":"item","steps":[steps]}
Templating: values are Jinja2. "{{ expr }}" alone returns the native value. Context: inputs.*, steps.<id> (that step's output),
the foreach variable, loop.index, today(), now_iso(), days_from_today(n). Filters: tojson, truncate_chars(n).
Rules:
- Prefer deterministic tool steps. Use llm only for understanding unstructured text; use decide for fast classification/ranking.
- Anything that sends, submits, publishes, purchases or deletes must use a tool whose side_effect is external, or be preceded by an approval step.
- Declare the minimum permissions. Every tool used must appear in permissions.tools.
- Include at least one test with mocks for every network/desktop/browser/llm step so it can run offline.
"""

EXAMPLE = {
    "id": "news.daily_digest", "name": "Daily digest", "description": "Fetch a page every morning and save a summary",
    "triggers": [{"type": "cron", "cron": "0 7 * * *"}, {"type": "manual"}],
    "inputs": {"url": {"type": "string", "default": "https://example.com/news"}},
    "permissions": {"tools": ["http.get", "fs.write_text", "notify.user"], "net": ["example.com"],
                    "fs_write": ["${FORGE_HOME}/workspace/digest"]},
    "steps": [
        {"kind": "tool", "id": "fetch", "tool": "http.get", "args": {"url": "{{ inputs.url }}"}, "retry": {"max_attempts": 3}},
        {"kind": "llm", "id": "summarise", "capability": "extraction",
         "prompt": "Summarise the key items:\n{{ steps.fetch.text | truncate_chars(6000) }}",
         "schema": {"type": "object", "properties": {"items": {"type": "array", "items": {"type": "string"}}}, "required": ["items"]}},
        {"kind": "tool", "id": "save", "tool": "fs.write_text",
         "args": {"path": "${FORGE_HOME}/workspace/digest/{{ today() }}.md", "text": "{{ steps.summarise['items'] | join('\\n- ') }}"}},
        {"kind": "tool", "id": "tell", "tool": "notify.user", "args": {"title": "Digest ready", "body": "{{ steps.summarise['items'] | length }} items"}}],
    "outputs": {"count": "{{ steps.summarise['items'] | length }}"},
    "tests": [{"name": "summarises", "mocks": {"http.get": {"text": "A happened. B happened."}, "llm": {"items": ["A", "B"]}},
               "expect": ["status == 'succeeded'", "outputs.count == 2"]}],
}


@dataclass
class CompileResult:
    ok: bool
    request: str
    workflow_id: str | None = None
    version_id: int | None = None
    version: int | None = None
    issues: list[dict] = field(default_factory=list)
    missing_tools: list[dict] = field(default_factory=list)
    tests: list[dict] = field(default_factory=list)
    dry_run: dict | None = None
    approval_id: str | None = None
    attempts: int = 0
    yaml: str = ""

    def summary(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if k != "request"}


class Compiler:
    def __init__(self, services):
        self.s = services

    def _catalog(self) -> str:
        lines = []
        for t in self.s.registry.all():
            if t.name.startswith("workflows."):
                continue
            props = t.json_schema().get("properties", {})
            req = set(t.json_schema().get("required", []))
            args = ", ".join(f"{k}{'' if k in req else '?'}: {v.get('type', 'any')}" for k, v in props.items())
            lines.append(f"- {t.name}({args}) — {t.description} [side_effect={t.side_effect}]")
        cols = "\n".join(f"- {c.name}: fields {[f.name for f in c.fields]}" + (f"; status {c.status_field} in {c.field(c.status_field).enum}" if c.status_field else "")
                         for c in self.s.records.collections.values())
        return "Available tools:\n" + "\n".join(lines) + ("\n\nRecord collections:\n" + cols if cols else "")

    async def compile(self, request: str, requested_by: str = "user", deploy: bool = True) -> CompileResult:
        res = CompileResult(ok=False, request=request)
        mem = await self.s.memory.context_block(request, k=6)
        messages = [
            {"role": "system", "content": "You are Forge's automation compiler. You output only JSON workflow definitions.\n\n" + IR_GUIDE},
            {"role": "user", "content": f"{self._catalog()}\n\nExample workflow:\n{json.dumps(EXAMPLE)}\n\n{mem}\n\n"
                                        f"FORGE_HOME is {self.s.cfg.home}. Use ${{FORGE_HOME}} in paths.\n\nAutomation request:\n{request}"}]
        ir: WorkflowIR | None = None
        for attempt in range(1, MAX_REPAIRS + 2):
            res.attempts = attempt
            out = await self.s.router.generate("compile", messages, schema="json", temperature=0.1, max_tokens=4000)  # type: ignore[arg-type]
            try:
                data = out.json()
            except json.JSONDecodeError as e:
                issues = [{"path": "$", "code": "json", "message": str(e)}]
                data = None
            if data is not None:
                res.missing_tools = data.pop("missing_tools", None) or []
                try:
                    ir = WorkflowIR.model_validate(data)
                    issues = [i.model_dump() for i in check_ir(ir, self.s.registry)]
                except ValidationError as e:
                    issues = [{"path": ".".join(map(str, er["loc"])), "code": er["type"], "message": er["msg"]} for er in e.errors()[:20]]
            res.issues = issues
            if not issues:
                break
            log.info("compile attempt %d: %d issues", attempt, len(issues))
            messages += [{"role": "assistant", "content": out.content},
                         {"role": "user", "content": "The workflow has compile errors. Fix ALL of them and return the complete corrected JSON:\n"
                                                     + "\n".join(f"- {i['path']}: {i['message']}" for i in issues)}]
        if res.issues or ir is None:
            return res
        if res.missing_tools:
            return res   # tool generation pipeline handles this (see docs/ROADMAP.md)
        for test_round in range(2):
            try:
                v = self.s.workflows.create_version(ir, created_by="compiler", note=request[:500])
            except CompileError as e:
                res.issues = [i.model_dump() for i in e.issues]
                return res
            res.workflow_id, res.version_id, res.version, res.yaml = ir.id, v["id"], v["version"], ir.to_yaml()
            res.tests = await self.s.workflows.run_tests(v["id"])
            failed = [t for t in res.tests if not t["passed"]]
            if not failed or test_round == 1:
                break
            # One repair round. The model may fix the workflow or a wrong expectation, but may not delete the safety net:
            # at least one test must remain and assert success.
            messages += [{"role": "assistant", "content": ir.model_dump_json(by_alias=True)},
                         {"role": "user", "content": "Tests failed:\n" + json.dumps(failed, default=str)[:3000] +
                          "\nFix the workflow or the incorrect expectations (expectations are plain Jinja expressions evaluated against "
                          "`status`, `outputs` and `steps`; do not put {{ }} inside them). Keep at least one test asserting "
                          "status == 'succeeded'. Return the complete corrected JSON."}]
            out = await self.s.router.generate("compile", messages, schema="json", temperature=0.1, max_tokens=4000)  # type: ignore[arg-type]
            try:
                data = out.json()
                data.pop("missing_tools", None)
                fixed = WorkflowIR.model_validate(data)
            except (json.JSONDecodeError, ValidationError):
                break
            if not fixed.tests or not any("status == 'succeeded'" in e or 'status == "succeeded"' in e for t in fixed.tests for e in t.expect):
                break
            if check_ir(fixed, self.s.registry):
                break
            ir = fixed
            res.attempts += 1
        res.dry_run = await self.s.workflows.dry_run(res.version_id)
        # Required gate (Forge-defined): every test scenario runs to success with its mocks, and the dry-run passes.
        # Model-written expectations are advisory: an 8B model often writes wrong ones (hard-coded dates, wrong mock
        # shapes), so failures are surfaced to the approver instead of silently blocking or silently passing.
        scenarios_ok = bool(res.tests) and all(t["status"] == "succeeded" for t in res.tests)
        advisory = [e for t in res.tests for e in t["failed_expectations"]]
        dry_ok = res.dry_run["status"] in ("succeeded", "waiting_user")
        res.ok = scenarios_ok and dry_ok
        if res.ok and deploy:
            ap = self.s.workflows.request_deploy(res.version_id, requested_by, {
                "tests": res.tests, "dry_run_status": res.dry_run["status"],
                "advisory_expectation_failures": advisory, "note": "review advisory failures before approving" if advisory else ""})
            res.approval_id = ap["id"]
        return res
