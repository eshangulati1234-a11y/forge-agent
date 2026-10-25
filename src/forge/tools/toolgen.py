"""Self-building tools: missing capability -> contract -> generated code + tests -> static checks -> isolated test run
-> signed approval -> versioned registration.

Day-1 scope and honest limits:
  * Generated tools are PURE COMPUTE: parse, transform, score, format. An AST allowlist rejects imports outside a
    small stdlib set and any I/O / reflection builtins. Tools that need files, network or the desktop are built by
    composing existing policy-checked tools in a workflow instead.
  * Every test run and every production call executes in a separate `python -I` subprocess with a stripped
    environment, a temp working dir, a timeout and a Windows Job Object memory cap.
  * This is defence in depth, NOT a security boundary against a determined adversary (AST filters can be evaded
    in principle). Phase 2 moves execution into Windows Sandbox / an AppContainer with no network.
  * Registration always requires an explicit, signed user approval showing the code hash.
"""
from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from forge.db import sha
from forge.errors import ForgeError, InvalidInput
from forge.tools.registry import SideEffect, ToolSpec, tool

ALLOWED_IMPORTS = {"json", "re", "math", "datetime", "statistics", "itertools", "collections", "csv", "html", "string",
                   "textwrap", "decimal", "fractions", "hashlib", "base64", "functools", "operator", "difflib", "unicodedata",
                   "urllib.parse", "dataclasses", "typing", "enum", "random", "bisect", "heapq", "calendar"}
BANNED_NAMES = {"eval", "exec", "compile", "open", "__import__", "globals", "locals", "vars", "getattr", "setattr", "delattr",
                "input", "breakpoint", "memoryview", "help", "exit", "quit"}
TIMEOUT_S = 20
MEM_LIMIT_MB = 512

RUNNER = r"""
import json, sys, importlib.util, traceback
spec = importlib.util.spec_from_file_location("gen_tool", sys.argv[1]); m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
mode = sys.argv[2]
if mode == "test":
    results = []
    for name in [n for n in dir(m) if n.startswith("test_")]:
        try:
            getattr(m, name)(); results.append({"test": name, "ok": True})
        except Exception as e:
            results.append({"test": name, "ok": False, "error": f"{type(e).__name__}: {e}"})
    print(json.dumps({"results": results}))
else:
    args = json.loads(sys.stdin.read())
    print(json.dumps({"result": m.run(args)}, default=str))
"""


def static_check(code: str) -> list[str]:
    problems = []
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return [f"syntax error: {e}"]
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name not in ALLOWED_IMPORTS:
                    problems.append(f"import of '{a.name}' is not allowed (pure-compute allowlist)")
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "") not in ALLOWED_IMPORTS:
                problems.append(f"import from '{node.module}' is not allowed")
        elif isinstance(node, ast.Name) and node.id in BANNED_NAMES:
            problems.append(f"use of '{node.id}' is not allowed")
        elif isinstance(node, ast.Attribute) and node.attr.startswith("__") and node.attr.endswith("__"):
            problems.append(f"dunder attribute access '{node.attr}' is not allowed")
    if not any(isinstance(n, ast.FunctionDef) and n.name == "run" for n in tree.body):
        problems.append("module must define run(args: dict) -> dict")
    if not any(isinstance(n, ast.FunctionDef) and n.name.startswith("test_") for n in tree.body):
        problems.append("module must define at least one test_* function")
    return problems


def _job_limited(proc: subprocess.Popen) -> None:
    try:
        import win32api
        import win32con
        import win32job
        job = win32job.CreateJobObject(None, "")
        info = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
        info["ProcessMemoryLimit"] = MEM_LIMIT_MB * 1024 * 1024
        info["BasicLimitInformation"]["LimitFlags"] = (win32job.JOB_OBJECT_LIMIT_PROCESS_MEMORY | win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
                                                       | win32job.JOB_OBJECT_LIMIT_ACTIVE_PROCESS)
        info["BasicLimitInformation"]["ActiveProcessLimit"] = 1      # no child processes
        win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, info)
        h = win32api.OpenProcess(win32con.PROCESS_SET_QUOTA | win32con.PROCESS_TERMINATE, False, proc.pid)
        win32job.AssignProcessToJobObject(job, h)
        proc._forge_job = job  # keep the handle alive for the process lifetime
    except Exception:
        pass


def run_isolated(module_path: Path, mode: str, payload: Any = None) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        runner = Path(tmp) / "runner.py"
        runner.write_text(RUNNER)
        env = {"SYSTEMROOT": os.environ.get("SYSTEMROOT", r"C:\Windows"), "PYTHONHASHSEED": "0"}   # stripped environment
        # The venv python.exe is a launcher that spawns a child, which the job's no-children limit forbids; run the base
        # interpreter directly. -I ignores site-packages and PYTHON* env vars, so only the stdlib is importable.
        exe = getattr(sys, "_base_executable", None) or sys.executable
        proc = subprocess.Popen([exe, "-I", str(runner), str(module_path), mode], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, cwd=tmp, env=env, text=True, creationflags=0x08000000)
        _job_limited(proc)
        try:
            out, err = proc.communicate(json.dumps(payload or {}), timeout=TIMEOUT_S)
        except subprocess.TimeoutExpired:
            proc.kill()
            raise ForgeError(f"generated tool exceeded {TIMEOUT_S}s")
        if proc.returncode != 0:
            raise ForgeError(f"generated tool crashed: {err.strip()[-800:]}")
        return json.loads(out.strip().splitlines()[-1])


def tool_dir(home: Path, name: str) -> Path:
    return home / "tools" / "generated" / name


class Contract(BaseModel):
    name: str = Field(description="tool name without prefix, lower_snake, e.g. parse_salary_range")
    description: str
    input_schema: dict = Field(description="JSON schema of the args dict")
    output_description: str
    examples: list[dict] = Field(default_factory=list, description="[{input: {...}, output: {...}}]")


async def generate_tool(services, contract: Contract, max_rounds: int = 3) -> dict:
    import re
    if not re.match(r"^[a-z][a-z0-9_]{2,40}$", contract.name):
        raise InvalidInput("tool name must be lower_snake_case")
    existing = services.registry.search(contract.description, 5)
    prompt = (f"Write a pure-Python module implementing this tool contract:\n{contract.model_dump_json(indent=1)}\n\n"
              f"Rules: define run(args: dict) -> dict; define >= 3 test_* functions using assert, covering the examples and edge cases. "
              f"Only these imports are allowed: {sorted(ALLOWED_IMPORTS)}. No file, network, OS, eval/exec, getattr, dunder access.\n"
              "Return JSON {\"code\": \"<entire module source>\"}.")
    messages = [{"role": "system", "content": "You write small, correct, dependency-free Python utilities with tests."},
                {"role": "user", "content": prompt}]
    report: dict[str, Any] = {"name": contract.name, "similar_existing": [t.name for t in existing], "rounds": []}
    for r in range(max_rounds):
        # qwen3 thinking mode materially improves code correctness; thinking tokens count against num_predict.
        out = await services.router.generate("coding", messages, schema="json", temperature=0.2, max_tokens=8000, think=True)
        try:
            code = out.json()["code"]
        except Exception:
            messages += [{"role": "assistant", "content": out.content}, {"role": "user", "content": "Return valid JSON {\"code\": ...}."}]
            continue
        problems = static_check(code)
        rnd: dict[str, Any] = {"round": r + 1, "static_problems": problems}
        if not problems:
            with tempfile.TemporaryDirectory() as tmp:
                mp = Path(tmp) / "gen_tool.py"
                mp.write_text(code, encoding="utf-8")
                try:
                    res = run_isolated(mp, "test")["results"]
                except ForgeError as e:
                    res = [{"test": "<module>", "ok": False, "error": str(e)}]
            rnd["tests"] = res
            if res and all(t["ok"] for t in res):
                report["rounds"].append(rnd)
                return _stage(services, contract, code, report)
            problems = [f"{t['test']}: {t.get('error')}" for t in res if not t["ok"]] or ["no tests ran"]
        report["rounds"].append(rnd)
        messages += [{"role": "assistant", "content": json.dumps({"code": code})},
                     {"role": "user", "content": "Fix these problems and return the full module as JSON:\n" + "\n".join(problems)}]
    report["ok"] = False
    return report


def _stage(services, contract: Contract, code: str, report: dict) -> dict:
    d = tool_dir(services.cfg.home, contract.name)
    versions = sorted(int(p.name[1:]) for p in d.glob("v*")) if d.exists() else []
    vdir = d / f"v{(versions[-1] + 1) if versions else 1}"
    vdir.mkdir(parents=True)
    (vdir / "tool.py").write_text(code, encoding="utf-8")
    code_hash = sha(code)
    manifest = {"name": f"gen.{contract.name}", "contract": contract.model_dump(), "code_sha256": code_hash, "version": vdir.name,
                "isolation": "subprocess -I, stripped env, job object (512MB, no children), 20s timeout", "approved": False}
    (vdir / "manifest.json").write_text(json.dumps(manifest, indent=2))
    ap = services.approvals.request("permission", f"register generated tool gen.{contract.name} ({vdir.name})",
                                    {"type": "register_tool", "name": f"gen.{contract.name}", "code_sha256": code_hash,
                                     "path": str(vdir), "code": code[:6000], "tests": report["rounds"][-1]["tests"]}, requested_by="toolgen")
    report.update(ok=True, staged=str(vdir), approval_id=ap["id"], code_sha256=code_hash)
    return report


def load_generated(services) -> list[str]:
    """Register generated tools whose staged code hash matches a signed, approved register_tool approval."""
    root = services.cfg.home / "tools" / "generated"
    added = []
    if not root.exists():
        return added
    for d in sorted(root.iterdir()):
        for vdir in sorted(d.glob("v*"), key=lambda p: int(p.name[1:]), reverse=True):
            m = json.loads((vdir / "manifest.json").read_text())
            code = (vdir / "tool.py").read_text(encoding="utf-8")
            if sha(code) != m["code_sha256"]:
                continue   # tampered on disk
            row = services.db.one("SELECT * FROM approvals WHERE kind='permission' AND status='approved' AND subject_json LIKE ? "
                                  "ORDER BY decided_at DESC LIMIT 1", (f'%"code_sha256":"{m["code_sha256"]}"%',))
            if not row or not services.approvals.verify(row):
                continue
            register_generated(services, m, vdir / "tool.py")
            added.append(m["name"])
            break
    return added


def register_generated(services, manifest: dict, path: Path) -> None:
    from forge.tools.mcp_bridge import _model_for

    def call(args: BaseModel, ctx) -> Any:
        return run_isolated(path, "run", args.model_dump())["result"]

    services.registry.register(ToolSpec(name=manifest["name"], description=f"[generated {manifest['version']}] {manifest['contract']['description']}",
                                        input_model=_model_for(manifest["contract"]["input_schema"], manifest["name"]), fn=call,
                                        side_effect=SideEffect.NONE, source="generated"))


class GenBody(Contract):
    pass


@tool("tools.generate", GenBody, description="Create a missing pure-compute tool: generates code + tests, checks and runs them in isolation, then asks you to approve registration",
      side_effect=SideEffect.LOCAL_WRITE, mock=lambda a, ctx: {"dry_run": True})
async def tools_generate(a: GenBody, ctx):
    return await generate_tool(ctx.services, Contract(**a.model_dump()))
