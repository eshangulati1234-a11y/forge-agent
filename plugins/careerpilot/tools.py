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
