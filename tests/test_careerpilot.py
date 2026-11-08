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


async def test_linkedin_workflow_checks_and_tests_pass(services):
    services.load_plugins(ROOT)
    assert "linkedin_jobs" in services.records.collections
    path = next(p for p in plugin_workflows(ROOT) if p.name == "linkedin_ai_jobs.yaml")
    ir = WorkflowIR.from_yaml(path.read_text(encoding="utf-8"))
    assert check_ir(ir, services.registry) == []
    v = services.workflows.create_version(ir, "user")
    res = await services.workflows.run_tests(v["id"])
    assert res and all(t["passed"] for t in res), res


def _check(services, job, page, url="https://www.linkedin.com/jobs/view/9/"):
    from forge.tools.registry import ToolContext
    services.load_plugins(ROOT)
    spec = services.registry.get("careerpilot.check_linkedin_job")
    base = {"company": "Acme", "job_title": "ML Engineer", "visa_sponsorship": "Not stated",
            "international_students_accepted": "Not stated", "work_rights_evidence": ""}
    return spec.fn(spec.validate({"job": base | job, "page_text": page, "url": url}), ToolContext(services))


def test_work_rights_need_real_relevant_evidence(services):
    page = "About the job. A Paypal account to receive payment from our clients. Full-time contract role."
    # Live run 1: visa "No" justified by a PayPal sentence.
    out = _check(services, {"visa_sponsorship": "No", "work_rights_evidence": "A Paypal account to receive payment from our clients"}, page)
    assert out["job"]["visa_sponsorship"] == "Not stated" and out["notes"]
    # "Likely" is only for part-time / casual / internship roles.
    out = _check(services, {"international_students_accepted": "Likely"}, page)
    assert out["job"]["international_students_accepted"] == "Not stated" and out["job"]["priority"] == 3
    # Evidence the model made up is dropped.
    out = _check(services, {"visa_sponsorship": "Yes", "work_rights_evidence": "We sponsor visas."}, page)
    assert out["job"]["visa_sponsorship"] == "Not stated" and out["job"]["work_rights_evidence"] == ""


def test_work_rights_kept_when_evidence_is_verbatim(services):
    page = ("This role requires the successful applicant to be an Australian citizen and hold at least a Baseline clearance. "
            "Visa sponsorship is available for exceptional candidates. Casual role.")
    out = _check(services, {"international_students_accepted": "No",
                            "work_rights_evidence": "This role requires the successful applicant to be an Australian citizen"}, page)
    assert out["job"]["international_students_accepted"] == "No" and not out["notes"]
    out = _check(services, {"visa_sponsorship": "Yes", "work_rights_evidence": "Visa sponsorship is available for exceptional candidates."}, page)
    assert out["job"]["visa_sponsorship"] == "Yes" and out["job"]["priority"] == 1
    assert _check(services, {"international_students_accepted": "Likely"}, page)["job"]["priority"] == 2


def test_duplicate_company_and_title_is_flagged(services):
    services.load_plugins(ROOT)
    services.records.upsert("linkedin_jobs", {"job_title": "ML Engineer (MSc. or PhD)", "company": "Synthires",
                                              "apply_link": "https://www.linkedin.com/jobs/view/1/"}, actor="t")
    dup = _check(services, {"company": "Synthires", "job_title": "ML Engineer (MSc or PhD)"}, "x",
                 url="https://www.linkedin.com/jobs/view/2/")
    assert dup["duplicate_of"]
    same_link = _check(services, {"company": "Synthires", "job_title": "ML Engineer (MSc. or PhD)"}, "x",
                       url="https://www.linkedin.com/jobs/view/1/")
    assert same_link["duplicate_of"] is None      # re-reading the same posting updates it, not a duplicate
