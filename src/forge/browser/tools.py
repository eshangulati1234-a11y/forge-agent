"""Browser tools (need permissions.browser). Workflows are confined to permissions.net; interactive sessions are not
host-restricted for navigation but every open still goes through the policy url check."""
from __future__ import annotations

import asyncio
from typing import Any, Literal

from pydantic import BaseModel, Field

from forge.browser.agent import BrowserManager
from forge.errors import NeedsUser
from forge.tools.registry import SideEffect, ToolContext, tool

_MGR: BrowserManager | None = None
PROFILE = Field(None, description="browser profile (persistent logins); omit to stay on the last opened one")


def mgr(ctx: ToolContext) -> BrowserManager:
    global _MGR
    if _MGR is None:
        cfg = ctx.services.cfg.browser
        _MGR = BrowserManager(ctx.services.cfg.home, cfg.executable, headless=cfg.headless,
                              show_cursor=cfg.show_cursor, action_delay_ms=cfg.action_delay_ms)
    return _MGR


def _allow(ctx: ToolContext) -> list[str] | None:
    perms = ctx.extra.get("permissions")
    return list(perms.net) if perms is not None and ctx.run_id else None


class Open(BaseModel):
    url: str
    new_tab: bool = Field(False, description="true opens this URL in a separate tab, preserving existing tabs")
    profile: str | None = PROFILE
    headless: bool | None = Field(None, description="omit to use browser settings (visible by default); true hides the window; launch mode changes require restart")
    wait_s: float = Field(0, ge=0, le=60, description="pause before loading (be polite to rate-limited sites)")
    require_login: bool = Field(False, description="stop and ask the user to log in if the page shows a login wall")


@tool("browser.open", Open, description="Open a URL in the Forge browser (persistent profile); detects CAPTCHA/login walls",
      caps={"browser", "net"}, url_fields=("url",), resource="browser")
async def open_(a: Open, ctx: ToolContext):
    if a.wait_s:
        await asyncio.sleep(a.wait_s)
    res = await mgr(ctx).open(a.url, a.profile, headless=a.headless, allow_hosts=_allow(ctx), new_tab=a.new_tab)
    if a.require_login and res.get("login_required"):
        raise NeedsUser(f"{res['url']} wants you to log in. Log in in the Forge browser window (profile "
                        f"'{mgr(ctx).current}'), then continue.")
    return res


class Snap(BaseModel):
    profile: str | None = PROFILE
    max_items: int = Field(150, ge=10, le=500)


@tool("browser.snapshot", Snap, description="Interactive elements (id, role, name, value) + page text of the current page", caps={"browser"})
async def snapshot(a: Snap, ctx: ToolContext):
    return await mgr(ctx).snapshot(a.profile, a.max_items)


class Act(BaseModel):
    element_id: str = Field(description="data-forge-id from browser.snapshot, e.g. e12")
    action: str = Field(description="click | fill | select | check | uncheck | upload")
    value: str | None = Field(None, description="text for fill, option label for select, file path for upload")
    profile: str | None = PROFILE


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


class Scroll(BaseModel):
    direction: Literal["down", "up", "top", "bottom"] = "down"
    pages: float = Field(1.0, gt=0, le=10, description="how far, in screen heights (down/up only)")
    element_id: str | None = Field(None, description="scroll until this snapshot element is in view (overrides direction)")
    profile: str | None = PROFILE


@tool("browser.scroll", Scroll, description="Scroll the current page like a mouse wheel (also scrolls inner panes). Returns "
      "scroll_pct and at_bottom. Clicks and fills already scroll their element into view", caps={"browser"}, resource="browser")
async def scroll(a: Scroll, ctx: ToolContext):
    return await mgr(ctx).scroll(a.profile, a.direction, a.pages, a.element_id)


class Tabs(BaseModel):
    switch_to: int | None = Field(None, ge=0, description="tab index to bring to the front and work in; omit to just list tabs")
    profile: str | None = PROFILE


@tool("browser.tabs", Tabs, description="List the browser's tabs (index, title, url, active), or switch to one. Forge follows "
      "the tab the user selects and tabs opened by links automatically", caps={"browser"}, resource="browser")
async def tabs(a: Tabs, ctx: ToolContext):
    return await mgr(ctx).tabs(a.profile, a.switch_to)


class Submit(BaseModel):
    element_id: str
    description: str = Field(description="what is being submitted, shown to the user in the approval")
    profile: str | None = PROFILE


@tool("browser.submit", Submit, description="Click a submit/send/apply/purchase control. External action: always requires approval",
      side_effect=SideEffect.EXTERNAL, caps={"browser"}, resource="browser")
async def submit(a: Submit, ctx: ToolContext):
    res = await mgr(ctx).act(a.profile, a.element_id, "click", allow_risky=True)
    snap = await mgr(ctx).snapshot(a.profile, 20, 1500)
    return {**res, "page_text_after": snap["text"][:1500]}


class Find(BaseModel):
    description: str = Field(description="what to find, e.g. 'the search box' or 'link to graduate jobs'")
    profile: str | None = PROFILE
    role: str | None = None


FIND_HINT = "browser.find only locates ONE link/button/field to act on; to read page text use browser.read"


@tool("browser.find", Find, description="Locate ONE interactive element (link, button, field) to click or fill (Laya System 1, LLM fallback). "
      "Cannot read text: use browser.read for page content", caps={"browser"})
async def find(a: Find, ctx: ToolContext):
    snap = await mgr(ctx).snapshot(a.profile, 200, 500)
    els = [e for e in snap["elements"] if not a.role or e["role"] == a.role]
    if not els:
        return {"found": False, "hint": FIND_HINT}
    # An explicit "none" lets System 2 refuse; without it, asking for "the job description text" returned
    # whichever link was least wrong, as found=True, and the model kept rephrasing.
    none = {"none": "none of these elements matches"}
    options = {e["id"]: f"{e['role']} '{e['name']}'" for e in els if e["name"]} | none
    laya_opts = {e["id"]: e["name"] for e in els if e["name"]} | none
    d = await ctx.services.decisions.choose(f"Page: {snap['title']}\nLooking for: {a.description}", f"Which element is {a.description}?",
                                           options, goal=a.description, laya_state=f"Find: {a.description}", laya_options=laya_opts,
                                           use_laya=ctx.services.laya is not None)
    el = next((e for e in els if e["id"] == d["choice"]), None)
    if el is None:
        return {"found": False, "hint": FIND_HINT, "system": d["system"]}
    return {"found": True, "element": el, "system": d["system"], "confidence": d.get("confidence")}


class Read(BaseModel):
    profile: str | None = PROFILE
    url: str | None = Field(None, description="page you expect to read; opened first if the browser is elsewhere")
    selector: str | None = Field(None, description="CSS selector to read instead of the auto-detected main content")
    max_chars: int = Field(8000, ge=500, le=40000)
    links: str | None = Field(None, description="regex; also return links whose URL matches it, e.g. '/jobs/view/\\\\d+'")
    scroll: bool = Field(False, description="scroll the page and its scroll panes first so lazy-loaded items render")
    wait_for: str | None = Field(None, description="wait until this text is on the page (content that loads late, e.g. 'About the job')")
    wait_s: float = Field(15, ge=1, le=60, description="how long to wait for wait_for / the page to settle")


@tool("browser.read", Read, description="Read the current page's main text (navigation, footers and sidebars removed); "
      "optionally list links matching a pattern", caps={"browser"}, url_fields=("url",))
async def read(a: Read, ctx: ToolContext):
    return await mgr(ctx).read(a.profile, selector=a.selector, max_chars=a.max_chars, links=a.links, scroll=a.scroll,
                                wait_for=a.wait_for, wait_s=a.wait_s,
                                url=a.url, allow_hosts=_allow(ctx))


class Download(BaseModel):
    element_id: str
    profile: str | None = PROFILE


@tool("browser.download", Download, description="Download an actual file attachment. To open web links use browser.open with new_tab=true, not this tool", side_effect=SideEffect.LOCAL_WRITE,
      caps={"browser"}, resource="browser", mock=lambda a, ctx: {"dry_run": True})
async def download(a: Download, ctx: ToolContext) -> dict[str, Any]:
    return await mgr(ctx).download(a.profile, a.element_id, ctx.services.cfg.home / "artifacts" / "downloads")


class Shot(BaseModel):
    profile: str | None = PROFILE


@tool("browser.screenshot", Shot, description="Screenshot the current page", caps={"browser"})
async def screenshot(a: Shot, ctx: ToolContext):
    return {"path": await mgr(ctx).screenshot(a.profile, ctx.services.cfg.home / "artifacts" / "screens")}
