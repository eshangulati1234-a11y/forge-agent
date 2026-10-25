from pathlib import Path

from forge.plugins import plugin_workflows
from forge.workflows.ir import WorkflowIR, check_ir

ROOT = Path(__file__).resolve().parents[1] / "plugins"


async def test_plugin_loads_and_workflow_tests_pass(services):
    services.load_plugins(ROOT)
    assert "careerpilot" in services.plugins
    assert {"applications", "contacts", "interviews"} <= set(services.records.collections)
    path = next(p for p in plugin_workflows(ROOT) if p.name == "job_watch.yaml")
    ir = WorkflowIR.from_yaml(path.read_text(encoding="utf-8"))
    assert check_ir(ir, services.registry) == []
    v = services.workflows.create_version(ir, "user")
    res = await services.workflows.run_tests(v["id"])
    assert res and all(t["passed"] for t in res), res


def test_filter_rules(services):
    services.load_plugins(ROOT)
    spec = services.registry.get("careerpilot.filter_jobs")
    from forge.tools.registry import ToolContext
    jobs = [{"role": "Graduate Software Engineer", "location": "Melbourne"},
            {"role": "Senior Graduate Program Lead", "location": "Sydney"},
            {"role": "Junior Data Analyst", "location": "London"},
            {"role": "Graduate Accountant", "location": "Perth"}]
    out = spec.fn(spec.validate({"jobs": jobs}), ToolContext(services))
    assert [j["role"] for j in out["jobs"]] == ["Graduate Software Engineer"]
