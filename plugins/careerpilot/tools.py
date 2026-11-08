"""CareerPilot plugin tools. Deterministic, typed, and policy-checked like any other tool."""
from __future__ import annotations

import datetime as dt
import re
import shutil
from pathlib import Path
from typing import Any, Literal

import httpx
import yaml
from pydantic import BaseModel, Field

from forge.config import forge_home
from forge.errors import NeedsUser
from forge.tools.builtin import UA, html_to_text, robots_allows
from forge.tools.registry import ToolContext, tool

PLUGIN = Path(__file__).parent
SENIOR = re.compile(r"\b(senior|sr\.?|lead|principal|staff|manager|head of|director|architect|vp)\b", re.I)
JUNIOR = re.compile(r"\b(graduate|grad|junior|jr\.?|entry[- ]level|associate|intern(ship)?|early career|apprentice|trainee)\b", re.I)
AU = re.compile(r"\b(australia|sydney|melbourne|brisbane|perth|adelaide|canberra|hobart|darwin|nsw|vic|qld|remote[- ]?(au|anz|apac))\b", re.I)


class FetchBoard(BaseModel):
    url: str = Field(description="public job-board API URL, e.g. https://boards-api.greenhouse.io/v1/boards/<co>/jobs?content=true "
                                 "or https://api.lever.co/v0/postings/<co>?mode=json")
    kind: Literal["greenhouse", "lever"]
    employer: str


@tool("careerpilot.fetch_board", FetchBoard, description="Fetch postings from a public Greenhouse/Lever job-board API (robots.txt respected)",
      caps={"net"}, url_fields=("url",), source="plugin:careerpilot")
async def fetch_board(a: FetchBoard, ctx: ToolContext):
    async with httpx.AsyncClient(headers={"User-Agent": UA}, timeout=30, follow_redirects=True) as c:
        if not await robots_allows(c, a.url):
            from forge.errors import PermissionDenied
            raise PermissionDenied(f"robots.txt disallows {a.url}")
        r = await c.get(a.url)
        r.raise_for_status()
        data = r.json()
    jobs = []
    if a.kind == "greenhouse":
        for j in data.get("jobs", []):
            text, _ = html_to_text(_unescape(j.get("content") or ""), j.get("absolute_url", ""))
            jobs.append({"employer": a.employer, "role": j["title"].strip(), "location": (j.get("location") or {}).get("name", ""),
                         "job_url": j["absolute_url"], "source": "greenhouse", "posted": (j.get("updated_at") or "")[:10],
                         "description": text[:2500]})
    else:
        for j in data:
            cats = j.get("categories") or {}
            jobs.append({"employer": a.employer, "role": j["text"].strip(), "location": cats.get("location") or "",
                         "job_url": j["hostedUrl"], "source": "lever",
                         "posted": dt.datetime.fromtimestamp(j.get("createdAt", 0) / 1000).date().isoformat() if j.get("createdAt") else "",
                         "description": (j.get("descriptionPlain") or "")[:2500]})
    return {"count": len(jobs), "jobs": jobs}


def _unescape(s: str) -> str:
    import html
    return html.unescape(s)


class Filter(BaseModel):
    jobs: list[dict[str, Any]]
    require_junior: bool = Field(True, description="keep only graduate/junior/entry roles (title match)")
    require_australia: bool = True
    title_keywords: list[str] = Field(default_factory=lambda: ["engineer", "developer", "software", "data", "analyst", "it", "cloud", "security", "devops", "support"])


@tool("careerpilot.filter_jobs", Filter, description="Deterministic filter: Australian location, junior/graduate level, tech titles; drops senior roles",
      source="plugin:careerpilot")
def filter_jobs(a: Filter, ctx: ToolContext):
    kept, dropped = [], {"location": 0, "seniority": 0, "title": 0}
    for j in a.jobs:
        t = j.get("role", "")
        if a.require_australia and not AU.search(f"{j.get('location', '')}"):
            dropped["location"] += 1
            continue
        if SENIOR.search(t):
            dropped["seniority"] += 1
            continue
        if a.require_junior and not JUNIOR.search(t):
            dropped["seniority"] += 1
            continue
        if a.title_keywords and not any(re.search(rf"\b{re.escape(k)}", t, re.I) for k in a.title_keywords):
            dropped["title"] += 1
            continue
        kept.append(j)
    return {"jobs": kept, "kept": len(kept), "dropped": dropped}


class NewOnly(BaseModel):
    jobs: list[dict[str, Any]]


@tool("careerpilot.new_only", NewOnly, description="Drop jobs already tracked in the applications collection (by stable job ID)",
      caps={"records"}, source="plugin:careerpilot")
def new_only(a: NewOnly, ctx: ToolContext):
    store = ctx.services.records
    spec = store.spec("applications")
    fresh = [j for j in a.jobs if not store.get("applications", store.stable_id(spec, {k: j.get(k) for k in spec.key_fields}))]
    return {"jobs": fresh, "new": len(fresh), "already_tracked": len(a.jobs) - len(fresh)}


class Profile(BaseModel):
    pass


def profile_path() -> Path:
    return forge_home() / "careerpilot" / "profile.yaml"


@tool("careerpilot.profile", Profile, description="Load the candidate profile; only verified facts are returned as facts", source="plugin:careerpilot")
def profile(a: Profile, ctx: ToolContext):
    p = profile_path()
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(PLUGIN / "profile.example.yaml", p)
    d = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    facts = [f["text"] for f in d.get("facts", []) if f.get("verified")]
    summary = (f"Candidate in {d.get('location', '?')}; work rights: {d.get('work_rights') or 'unknown'}. "
               f"Targets: {', '.join(d.get('target_roles', []))}. Skills: {', '.join(d.get('skills', []))}. "
               f"Verified background: {'; '.join(facts) or 'none verified yet'}")
    return {"name": d.get("name"), "summary": summary, "verified_facts": facts, "skills": d.get("skills", []),
            "work_rights": d.get("work_rights") or "", "has_verified_facts": bool(facts), "path": str(p)}


class RequireVerified(BaseModel):
    profile: dict[str, Any]


@tool("careerpilot.require_verified_profile", RequireVerified,
      description="Stop with needs_user unless the profile has verified facts (truthfulness gate before drafting documents)",
      source="plugin:careerpilot")
def require_verified(a: RequireVerified, ctx: ToolContext):
    if not a.profile.get("has_verified_facts"):
        raise NeedsUser(f"Edit {a.profile.get('path')} and mark true facts `verified: true` before Forge drafts any documents.")
    return {"ok": True}


class ToRecord(BaseModel):
    job: dict[str, Any]
    score: dict[str, Any] = Field(description="output of a decide/score step")
    experience: dict[str, Any] = Field(description="output of a decide/yes_no step: requires >2y experience?")
    work_rights: str = ""


@tool("careerpilot.to_record", ToRecord, description="Build an applications record from a job + decisions (deterministic mapping)",
      source="plugin:careerpilot")
def to_record(a: ToRecord, ctx: ToolContext):
    j = a.job
    needs_exp = bool(a.experience.get("answer"))
    citizen_only = bool(re.search(r"\b(citizen(ship)?|baseline clearance|nv1|nv2|security clearance)\b", j.get("description", ""), re.I))
    elig = "Check" if needs_exp or (citizen_only and "citizen" not in a.work_rights.lower()) else "Eligible"
    return {"record": {"employer": j["employer"], "role": j["role"], "location": j.get("location") or None, "job_url": j["job_url"],
                       "source": j.get("source"), "date_discovered": dt.date.today().isoformat(),
                       "suitability_score": int(round(float(a.score.get("score", 0)) * 100)),
                       "suitability_level": a.score.get("level"), "eligibility": elig,
                       "experience_flag": ">2y experience asked" if needs_exp else "",
                       "notes": f"scored by System {a.score.get('system')}; posted {j.get('posted', '')}"}}


# Work-rights answers must rest on a real sentence from the posting. The first live run had visa="No" justified by
# "A Paypal account to receive payment", and "Likely" justified by "Based in an English-speaking country".
RIGHTS_TERMS = {
    "visa_sponsorship": re.compile(r"\b(visa|sponsor\w*)\b", re.I),
    "intl_no": re.compile(r"\b(citizen\w*|permanent residen\w*|PR|clearance|nv1|nv2|baseline|(full|unrestricted|valid|existing)\s+(working|work) rights"
                          r"|right to work|work(ing)? rights|eligib\w+ to work)\b", re.I),
    "intl_yes": re.compile(r"\b(international students?|student visa|visa holders?|subclass 500|graduate visa|485)\b", re.I),
}
PART_TIME = re.compile(r"\b(part[- ]time|casual|intern(ship)?|vacation(er)? program|student role)\b", re.I)


def _norm_text(s: str) -> str:
    return re.sub(r"\W+", " ", s or "").strip().lower()


class CheckLinkedInJob(BaseModel):
    job: dict[str, Any] = Field(description="output of the extraction step")
    page_text: str = Field(description="the posting text the extraction was made from")
    url: str
    collection: str = "linkedin_jobs"


@tool("careerpilot.check_linkedin_job", CheckLinkedInJob,
      description="Deterministic checks on an extracted LinkedIn job: work-rights answers need verbatim, relevant evidence; "
                  "flags duplicates (same company + title already tracked under another link)", source="plugin:careerpilot")
def check_linkedin_job(a: CheckLinkedInJob, ctx: ToolContext):
    j = dict(a.job)
    notes = []
    ev = (j.get("work_rights_evidence") or "").strip()
    ev_real = bool(ev) and _norm_text(ev)[:120] in _norm_text(a.page_text)
    if ev and not ev_real:
        notes.append(f"evidence not found in the posting: {ev[:80]!r}")
    visa, intl = j.get("visa_sponsorship"), j.get("international_students_accepted")
    if visa in ("Yes", "No") and not (ev_real and RIGHTS_TERMS["visa_sponsorship"].search(ev)):
        notes.append(f"visa_sponsorship {visa} -> Not stated (evidence doesn't mention visas/sponsorship)")
        visa = "Not stated"
    if intl == "No" and not (ev_real and RIGHTS_TERMS["intl_no"].search(ev)):
        notes.append("international_students_accepted No -> Not stated (evidence doesn't mention citizenship/PR/work rights)")
        intl = "Not stated"
    if intl == "Yes" and not (ev_real and RIGHTS_TERMS["intl_yes"].search(ev)):
        notes.append("international_students_accepted Yes -> Not stated (evidence doesn't mention international students/visas)")
        intl = "Not stated"
    if intl == "Likely" and not PART_TIME.search(f"{j.get('job_title', '')} {a.page_text}"):
        notes.append("international_students_accepted Likely -> Not stated (not a part-time/casual/internship role)")
        intl = "Not stated"
    relevant = ev_real and any(t.search(ev) for t in RIGHTS_TERMS.values())
    if ev_real and not relevant:
        notes.append(f"evidence dropped, not about work rights: {ev[:80]!r}")
    j.update(visa_sponsorship=visa, international_students_accepted=intl, work_rights_evidence=ev if relevant else "")
    j["priority"] = 1 if visa == "Yes" else 2 if intl in ("Yes", "Likely") else 3

    key = (_norm_text(j.get("company")), _norm_text(j.get("job_title")))
    dup = next((r for r in ctx.services.records.list(a.collection)
                if (_norm_text(r.get("company")), _norm_text(r.get("job_title"))) == key and r.get("apply_link") != a.url), None)
    return {"job": j, "duplicate_of": dup["id"] if dup else None, "notes": notes}
