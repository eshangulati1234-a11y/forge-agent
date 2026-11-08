"""Built-in tools. Each declares side effect + path/url fields so the policy engine can check them generically."""
from __future__ import annotations

import html
import re
import time
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import httpx
from pydantic import BaseModel, Field

from forge.errors import InvalidInput, NeedsUser, PermissionDenied
from forge.records import excel as xl
from forge.tools.registry import SideEffect, ToolContext, tool

UA = "ForgeAgent/0.1 (+self-hosted personal automation; respects robots.txt)"


# ---------------------------------------------------------------- filesystem
class ReadText(BaseModel):
    path: str
    max_chars: int = Field(20000, ge=1, le=500_000)


@tool("fs.read_text", ReadText, description="Read a UTF-8 text file", caps={"fs.read"}, path_fields=("path",))
def fs_read_text(a: ReadText, ctx: ToolContext):
    p = Path(a.path).expanduser()
    return {"path": str(p), "text": p.read_text(encoding="utf-8", errors="replace")[: a.max_chars]}


class ListDir(BaseModel):
    path: str
    pattern: str = "*"


@tool("fs.list", ListDir, description="List files in a directory", caps={"fs.read"}, path_fields=("path",))
def fs_list(a: ListDir, ctx: ToolContext):
    p = Path(a.path).expanduser()
    return {"files": [{"name": x.name, "is_dir": x.is_dir(), "size": x.stat().st_size if x.is_file() else None}
                      for x in sorted(p.glob(a.pattern))[:500]]}


class WriteText(BaseModel):
    path: str
    text: str
    overwrite: bool = True


def _write_mock(a: WriteText, ctx):
    return {"dry_run": True, "path": a.path, "chars": len(a.text)}


@tool("fs.write_text", WriteText, description="Write a UTF-8 text file (creates parent dirs)", side_effect=SideEffect.LOCAL_WRITE,
      caps={"fs.write"}, path_fields=("path",), mock=_write_mock)
def fs_write_text(a: WriteText, ctx: ToolContext):
    p = Path(a.path).expanduser()
    if p.exists() and not a.overwrite:
        raise InvalidInput(f"{p} exists and overwrite=false")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(a.text, encoding="utf-8")
    return {"path": str(p), "chars": len(a.text)}


# ---------------------------------------------------------------- http
_robots: dict[str, tuple[float, RobotFileParser | None]] = {}


async def robots_allows(client: httpx.AsyncClient, url: str) -> bool:
    u = urlparse(url)
    base = f"{u.scheme}://{u.netloc}"
    cached = _robots.get(base)
    if not cached or time.time() - cached[0] > 3600:
        rp: RobotFileParser | None = RobotFileParser()
        try:
            r = await client.get(base + "/robots.txt", timeout=10)
            if r.status_code >= 400:
                rp = None   # no robots.txt => allowed
            else:
                rp.parse(r.text.splitlines())
        except httpx.HTTPError:
            rp = None
        _robots[base] = cached = (time.time(), rp)
    rp = cached[1]
    return True if rp is None else rp.can_fetch(UA, url)


class _Text(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "head"}

    def __init__(self, base: str):
        super().__init__()
        self.base, self.out, self.links, self._skip, self._a = base, [], [], 0, None

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1
        if tag == "a":
            href = dict(attrs).get("href")
            self._a = [urljoin(self.base, href), []] if href else None
        if tag in ("p", "br", "li", "h1", "h2", "h3", "h4", "tr", "div", "section", "article"):
            self.out.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip:
            self._skip -= 1
        if tag == "a" and self._a:
            text = " ".join("".join(self._a[1]).split())
            if text:
                self.links.append({"text": text[:120], "url": self._a[0]})
            self._a = None

    def handle_data(self, data):
        if self._skip:
            return
        self.out.append(data)
        if self._a is not None:
            self._a[1].append(data)


def html_to_text(body: str, base: str) -> tuple[str, list[dict]]:
    p = _Text(base)
    p.feed(body)
    text = re.sub(r"[ \t]+", " ", html.unescape("".join(p.out)))
    text = re.sub(r"\n\s*\n+", "\n\n", text).strip()
    return text, p.links


class HttpGet(BaseModel):
    url: str
    as_text: bool = Field(True, description="convert HTML to readable text + links")
    max_chars: int = Field(30000, ge=100, le=400_000)
    respect_robots: bool = True
    headers: dict[str, str] = Field(default_factory=dict)


@tool("http.get", HttpGet, description="HTTP GET a URL (robots.txt respected); returns text/links or JSON", caps={"net"}, url_fields=("url",))
async def http_get(a: HttpGet, ctx: ToolContext):
    async with httpx.AsyncClient(headers={"User-Agent": UA, **a.headers}, follow_redirects=True, timeout=30) as c:
        if a.respect_robots and not await robots_allows(c, a.url):
            raise PermissionDenied(f"robots.txt disallows {a.url}; use an official API or user-assisted access")
        r = await c.get(a.url)
        perms = ctx.extra.get("permissions")
        if perms is not None and str(r.url) != a.url:
            from forge.policy.engine import host_allowed
            if not host_allowed(str(r.url), perms.net):
                raise PermissionDenied(f"redirected to non-allowlisted host {urlparse(str(r.url)).hostname}")
        if r.status_code in (401, 403) and ("captcha" in r.text.lower() or "cf-chl" in r.text):
            raise NeedsUser(f"{a.url} presented a bot challenge; Forge does not bypass these")
        r.raise_for_status()
        ctype = r.headers.get("content-type", "")
        if "json" in ctype:
            return {"url": str(r.url), "status": r.status_code, "json": r.json()}
        if a.as_text and "html" in ctype:
            text, links = html_to_text(r.text, str(r.url))
            return {"url": str(r.url), "status": r.status_code, "title": _title(r.text), "text": text[: a.max_chars], "links": links[:300]}
        return {"url": str(r.url), "status": r.status_code, "text": r.text[: a.max_chars]}


def _title(body: str) -> str:
    m = re.search(r"<title[^>]*>(.*?)</title>", body, re.S | re.I)
    return html.unescape(m.group(1).strip()) if m else ""


class HttpPost(BaseModel):
    url: str
    json_body: dict[str, Any] = Field(default_factory=dict)
    headers: dict[str, str] = Field(default_factory=dict)


@tool("http.post_json", HttpPost, description="POST JSON to an API (external side effect: approval required)",
      side_effect=SideEffect.EXTERNAL, caps={"net"}, url_fields=("url",))
async def http_post(a: HttpPost, ctx: ToolContext):
    async with httpx.AsyncClient(headers={"User-Agent": UA, **a.headers}, timeout=30) as c:
        r = await c.post(a.url, json=a.json_body)
        r.raise_for_status()
        return {"status": r.status_code, "body": r.text[:20000]}


# ---------------------------------------------------------------- records + excel
class RecUpsert(BaseModel):
    collection: str
    record: dict[str, Any]


@tool("records.upsert", RecUpsert, description="Insert or update a structured record (validated, versioned, audited)",
      side_effect=SideEffect.LOCAL_WRITE, caps={"records"}, mock=lambda a, ctx: {"dry_run": True, "id": a.record.get("id")})
def records_upsert(a: RecUpsert, ctx: ToolContext):
    return ctx.services.records.upsert(a.collection, a.record, actor=ctx.run_id or ctx.session_id or "agent")


class RecUpsertMany(BaseModel):
    collection: str
    records: list[dict[str, Any]]


@tool("records.upsert_many", RecUpsertMany, description="Upsert many records; invalid ones are reported, not fatal",
      side_effect=SideEffect.LOCAL_WRITE, caps={"records"}, mock=lambda a, ctx: {"dry_run": True, "count": len(a.records)})
def records_upsert_many(a: RecUpsertMany, ctx: ToolContext):
    out = {"created": [], "updated": [], "unchanged": [], "invalid": []}
    for rec in a.records:
        try:
            r = ctx.services.records.upsert(a.collection, rec, actor=ctx.run_id or ctx.session_id or "agent")
            (out["created"] if r["created"] else out["updated"] if r["changed"] else out["unchanged"]).append(r["id"])
        except InvalidInput as e:
            out["invalid"].append({"record": {k: rec.get(k) for k in list(rec)[:4]}, "error": str(e)})
    return out


class RecQuery(BaseModel):
    collection: str
    where: dict[str, Any] = Field(default_factory=dict)
    limit: int = Field(200, ge=1, le=5000)


@tool("records.query", RecQuery, description="List records in a collection, optionally filtered by exact field values", caps={"records"})
def records_query(a: RecQuery, ctx: ToolContext):
    rows = ctx.services.records.list(a.collection, a.where or None, a.limit)
    return {"count": len(rows), "records": rows}


class ExcelExport(BaseModel):
    collections: list[str]
    path: str


@tool("excel.export", ExcelExport, description="Export record collections to a formatted, verified Excel workbook (backup first)",
      side_effect=SideEffect.LOCAL_WRITE, caps={"fs.write"}, path_fields=("path",),
      mock=lambda a, ctx: {"dry_run": True, "path": a.path})
def excel_export(a: ExcelExport, ctx: ToolContext):
    return xl.export_collections(ctx.services.records, a.collections, Path(a.path))


class ExcelImport(BaseModel):
    collection: str
    path: str


@tool("excel.import", ExcelImport, description="Import user edits from an exported workbook (three-way merge; conflicts recorded)",
      side_effect=SideEffect.LOCAL_WRITE, caps={"fs.read", "records"}, path_fields=("path",),
      mock=lambda a, ctx: {"dry_run": True})
def excel_import(a: ExcelImport, ctx: ToolContext):
    return xl.import_collection(ctx.services.records, a.collection, Path(a.path))


class ExcelPath(BaseModel):
    path: str


class ExcelCreate(BaseModel):
    path: str
    sheet: str = Field("Jobs", min_length=1, max_length=31)
    headers: list[str] = Field(min_length=1)
    rows: list[list[Any]] = Field(default_factory=list, description="Data rows in the same column order as headers")


@tool("excel.create", ExcelCreate, description="Create a new formatted .xlsx spreadsheet from headers and rows; works without Excel running. Refuses to overwrite files",
      side_effect=SideEffect.LOCAL_WRITE, caps={"fs.write"}, path_fields=("path",),
      mock=lambda a, ctx: {"dry_run": True, "rows": len(a.rows)})
def excel_create(a: ExcelCreate, ctx: ToolContext):
    return xl.create_workbook(Path(a.path), a.sheet, a.headers, a.rows)


@tool("excel.open", ExcelPath, description="Open an existing .xlsx file in Microsoft Excel (finds Office even when excel.exe is not on PATH)",
      side_effect=SideEffect.LOCAL_WRITE, caps={"desktop", "fs.read"}, path_fields=("path",), resource="desktop",
      mock=lambda a, ctx: {"dry_run": True, "path": a.path})
async def excel_open(a: ExcelPath, ctx: ToolContext):
    import asyncio
    from forge.desktop.launch import open_workbook
    from forge.desktop.tools import LEASE, _attended
    async with LEASE.hold(ctx.run_id or ctx.session_id or "agent", attended=_attended(ctx)):
        return await asyncio.to_thread(open_workbook, Path(a.path))


@tool("excel.inspect", ExcelPath, description="Describe a workbook: sheets, headers, tables, formulas, validations, charts",
      caps={"fs.read"}, path_fields=("path",))
def excel_inspect(a: ExcelPath, ctx: ToolContext):
    return xl.inspect_workbook(Path(a.path))


class ExcelRead(BaseModel):
    path: str
    sheet: str | None = None
    cell_range: str | None = Field(None, description="e.g. A1:F20")
    max_rows: int = 200


@tool("excel.read", ExcelRead, description="Read cell values from a workbook range", caps={"fs.read"}, path_fields=("path",))
def excel_read(a: ExcelRead, ctx: ToolContext):
    return xl.read_range(Path(a.path), a.sheet, a.cell_range, a.max_rows)


class ExcelWrite(BaseModel):
    path: str
    sheet: str
    cells: dict[str, Any] = Field(description="{'B2': 'value', 'C5': 3}")


@tool("excel.write_cells", ExcelWrite, description="Write cells preserving styles (backup + verify; uses Excel COM for workbooks with charts)",
      side_effect=SideEffect.LOCAL_WRITE, caps={"fs.write"}, path_fields=("path",),
      mock=lambda a, ctx: {"dry_run": True, "cells": len(a.cells)})
def excel_write(a: ExcelWrite, ctx: ToolContext):
    return xl.write_cells(Path(a.path), a.sheet, a.cells)


# ---------------------------------------------------------------- memory / notify / text
class Remember(BaseModel):
    content: str
    kind: str = Field("fact", description="preference | fact | episode | skill | failure")


@tool("memory.remember", Remember, description="Store a memory. Agent-written memories are stored as unverified hypotheses",
      side_effect=SideEffect.LOCAL_WRITE, caps={"memory"}, mock=lambda a, ctx: {"dry_run": True})
async def memory_remember(a: Remember, ctx: ToolContext):
    prov = "tool_observed" if ctx.run_id else "generated"
    return await ctx.services.memory.add(a.kind, a.content, prov, {"source": ctx.run_id or ctx.session_id})


class Search(BaseModel):
    query: str
    k: int = 8


@tool("memory.search", Search, description="Semantic search over long-term memory", caps={"memory"})
async def memory_search(a: Search, ctx: ToolContext):
    return {"results": await ctx.services.memory.search(a.query, a.k)}


class Notify(BaseModel):
    title: str
    body: str = ""


@tool("notify.user", Notify, description="Notify the user locally (terminal + Windows toast). Not external.", caps=set())
def notify_user(a: Notify, ctx: ToolContext):
    if not ctx.dry_run:
        ctx.services.notify(a.title, a.body)
    return {"notified": not ctx.dry_run}


class ToolSearch(BaseModel):
    query: str


@tool("tools.search", ToolSearch, description="Find available tools by keyword (dynamic tool discovery)")
def tools_search(a: ToolSearch, ctx: ToolContext):
    return {"tools": [{"name": t.name, "description": t.description, "side_effect": str(t.side_effect)}
                      for t in ctx.services.registry.search(a.query, 12)]}


class JsonPick(BaseModel):
    data: Any
    fields: list[str]


@tool("data.pick", JsonPick, description="Keep only the given fields from each object in a list (deterministic transform)")
def data_pick(a: JsonPick, ctx: ToolContext):
    rows = a.data if isinstance(a.data, list) else [a.data]
    return {"rows": [{k: r.get(k) for k in a.fields} for r in rows if isinstance(r, dict)]}


# ---------------------------------------------------------------- workflows (agent meta-tools)
class WfList(BaseModel):
    pass


@tool("workflows.list", WfList, description="List workflows with deployed version and last run status")
def wf_list(a: WfList, ctx: ToolContext):
    return {"workflows": ctx.services.workflows.list()}


class WfRun(BaseModel):
    workflow_id: str
    inputs: dict[str, Any] = Field(default_factory=dict)


@tool("workflows.run", WfRun, description="Queue a run of a deployed workflow", side_effect=SideEffect.LOCAL_WRITE,
      mock=lambda a, ctx: {"dry_run": True})
def wf_run(a: WfRun, ctx: ToolContext):
    return {"run_id": ctx.services.runtime.enqueue(a.workflow_id, trigger="agent", inputs=a.inputs)}


class WfCompile(BaseModel):
    request: str = Field(description="the user's natural-language automation request, complete and specific")


@tool("workflows.compile", WfCompile, description="Compile a natural-language automation request into a tested workflow and request deploy approval",
      side_effect=SideEffect.LOCAL_WRITE, mock=lambda a, ctx: {"dry_run": True})
async def wf_compile(a: WfCompile, ctx: ToolContext):
    from forge.workflows.compiler import Compiler
    res = await Compiler(ctx.services).compile(a.request, requested_by=ctx.session_id or "agent")
    return res.summary()


class RunGet(BaseModel):
    run_id: str


@tool("workflows.run_status", RunGet, description="Get a run's status and step results")
def wf_run_status(a: RunGet, ctx: ToolContext):
    db = ctx.services.db
    run = db.one("SELECT id,workflow_id,status,error,error_class,outputs_json,created_at,finished_at FROM runs WHERE id=?", (a.run_id,))
    steps = db.all("SELECT step_path,status,error_class,substr(output_json,1,400) AS output FROM step_runs WHERE run_id=? ORDER BY started_at", (a.run_id,))
    return {"run": run, "steps": steps}
