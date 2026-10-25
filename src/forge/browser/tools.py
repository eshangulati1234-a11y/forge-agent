"""Browser tools (need permissions.browser). Workflows are confined to permissions.net; interactive sessions are not
host-restricted for navigation but every open still goes through the policy url check."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from forge.browser.agent import BrowserManager
from forge.tools.registry import SideEffect, ToolContext, tool

_MGR: BrowserManager | None = None


def mgr(ctx: ToolContext) -> BrowserManager:
    global _MGR
    if _MGR is None:
        _MGR = BrowserManager(ctx.services.cfg.home)
    return _MGR


def _allow(ctx: ToolContext) -> list[str] | None:
    perms = ctx.extra.get("permissions")
    return list(perms.net) if perms is not None and ctx.run_id else None


class Open(BaseModel):
    url: str
    profile: str = "default"
    headless: bool = Field(True, description="false shows the window (use for user-assisted login)")


@tool("browser.open", Open, description="Open a URL in the Forge browser (persistent profile); detects CAPTCHA/login walls",
      caps={"browser", "net"}, url_fields=("url",), resource="browser")
async def open_(a: Open, ctx: ToolContext):
    return await mgr(ctx).open(a.url, a.profile, headless=a.headless, allow_hosts=_allow(ctx))


class Snap(BaseModel):
    profile: str = "default"
    max_items: int = Field(150, ge=10, le=500)


@tool("browser.snapshot", Snap, description="Interactive elements (id, role, name, value) + page text of the current page", caps={"browser"})
async def snapshot(a: Snap, ctx: ToolContext):
    return await mgr(ctx).snapshot(a.profile, a.max_items)


class Act(BaseModel):
    element_id: str = Field(description="data-forge-id from browser.snapshot, e.g. e12")
    action: str = Field(description="click | fill | select | check | uncheck | upload")
    value: str | None = Field(None, description="text for fill, option label for select, file path for upload")
    profile: str = "default"


@tool("browser.act", Act, description="Act on a snapshot element. Refuses submit/send/buy controls (use browser.submit)",
      side_effect=SideEffect.LOCAL_WRITE, caps={"browser"}, resource="browser",
      mock=lambda a, ctx: {"dry_run": True, "ok": True})
async def act(a: Act, ctx: ToolContext):
    if a.action == "upload" and a.value:
        from forge.policy.engine import Permissions, _norm, _under
        perms: Permissions | None = ctx.extra.get("permissions")
        if perms is not None and not _under(_norm(a.value), perms.fs_read + perms.fs_write):
            from forge.errors import PermissionDenied
            raise PermissionDenied(f"upload of {a.value} is outside this workflow's readable paths")
    return await mgr(ctx).act(a.profile, a.element_id, a.action, a.value)


class Submit(BaseModel):
    element_id: str
    description: str = Field(description="what is being submitted, shown to the user in the approval")
    profile: str = "default"


@tool("browser.submit", Submit, description="Click a submit/send/apply/purchase control. External action: always requires approval",
      side_effect=SideEffect.EXTERNAL, caps={"browser"}, resource="browser")
async def submit(a: Submit, ctx: ToolContext):
    res = await mgr(ctx).act(a.profile, a.element_id, "click", allow_risky=True)
    snap = await mgr(ctx).snapshot(a.profile, 20, 1500)
    return {**res, "page_text_after": snap["text"][:1500]}


class Find(BaseModel):
    description: str = Field(description="what to find, e.g. 'the search box' or 'link to graduate jobs'")
    profile: str = "default"
    role: str | None = None


@tool("browser.find", Find, description="Find the element best matching a description (Laya System 1, LLM fallback)", caps={"browser"})
async def find(a: Find, ctx: ToolContext):
    snap = await mgr(ctx).snapshot(a.profile, 200, 500)
    els = [e for e in snap["elements"] if not a.role or e["role"] == a.role]
    if not els:
        return {"found": False}
    options = {e["id"]: f"{e['role']} '{e['name']}'" for e in els if e["name"]}
    laya_opts = {e["id"]: e["name"] for e in els if e["name"]}
    d = await ctx.services.decisions.choose(f"Page: {snap['title']}\nLooking for: {a.description}", f"Which element is {a.description}?",
                                           options, goal=a.description, laya_state=f"Find: {a.description}", laya_options=laya_opts,
                                           use_laya=ctx.services.laya is not None)
    el = next(e for e in els if e["id"] == d["choice"])
    return {"found": True, "element": el, "system": d["system"], "confidence": d.get("confidence")}


class Download(BaseModel):
    element_id: str
    profile: str = "default"


@tool("browser.download", Download, description="Click an element that triggers a download and save the file", side_effect=SideEffect.LOCAL_WRITE,
      caps={"browser"}, resource="browser", mock=lambda a, ctx: {"dry_run": True})
async def download(a: Download, ctx: ToolContext) -> dict[str, Any]:
    return await mgr(ctx).download(a.profile, a.element_id, ctx.services.cfg.home / "artifacts" / "downloads")


class Shot(BaseModel):
    profile: str = "default"


@tool("browser.screenshot", Shot, description="Screenshot the current page", caps={"browser"})
async def screenshot(a: Shot, ctx: ToolContext):
    return {"path": await mgr(ctx).screenshot(a.profile, ctx.services.cfg.home / "artifacts" / "screens")}
