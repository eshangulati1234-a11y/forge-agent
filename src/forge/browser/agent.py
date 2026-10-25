"""Playwright browser backend.

* One Chromium per daemon; one persistent context per profile (FORGE_HOME/browser/<profile>) so logins the user
  performs by hand persist. Forge never stores or types passwords; logging in is a user-assisted step.
* Snapshot = interactive elements tagged with data-forge-id, with role / accessible name / value / visibility,
  plus a readable text excerpt. Those elements are the grounded candidates for Laya or the LLM.
* Challenges (CAPTCHA, Cloudflare/Turnstile, "verify you are human") -> NeedsUser. No bypass, ever.
* Workflow navigation is confined to the workflow's net allowlist at the network layer (route interception of
  main-frame navigations).
* Unexpected JS dialogs: alerts accepted, confirms/prompts dismissed (the safe default), all recorded.
"""
from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path
from typing import Any

from forge.errors import InterfaceChanged, NeedsUser, PermissionDenied
from forge.policy.engine import host_allowed

SNAPSHOT_JS = r"""
(maxItems) => {
  const sel = 'a[href],button,input,select,textarea,[role=button],[role=link],[role=checkbox],[role=radio],[role=tab],[role=menuitem],[role=option],[contenteditable=true],summary';
  const out = [];
  let n = 0;
  for (const el of document.querySelectorAll(sel)) {
    const r = el.getBoundingClientRect();
    const st = getComputedStyle(el);
    const visible = r.width > 0 && r.height > 0 && st.visibility !== 'hidden' && st.display !== 'none';
    if (!visible || el.disabled) continue;
    if (!el.dataset.forgeId) el.dataset.forgeId = 'e' + (window.__forgeSeq = (window.__forgeSeq || 0) + 1);
    const lbl = el.labels && el.labels.length ? el.labels[0].innerText : '';
    const name = (el.getAttribute('aria-label') || lbl || el.innerText || el.value || el.placeholder || el.title || el.name || el.alt || '').trim().replace(/\s+/g, ' ').slice(0, 120);
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute('type') || '').toLowerCase();
    const role = el.getAttribute('role') || (tag === 'a' ? 'link' : tag === 'select' ? 'combobox' : tag === 'textarea' ? 'textbox'
                 : tag === 'input' ? ({checkbox:'checkbox', radio:'radio', submit:'button', button:'button', file:'file', password:'password'}[type] || 'textbox') : tag);
    out.push({id: el.dataset.forgeId, role, tag, type, name, href: el.href || null,
              value: (tag === 'input' || tag === 'textarea' || tag === 'select') ? (type === 'password' ? '***' : String(el.value || '').slice(0, 200)) : null,
              checked: (type === 'checkbox' || type === 'radio') ? el.checked : null,
              in_form: !!el.form, required: !!el.required,
              options: tag === 'select' ? Array.from(el.options).slice(0, 30).map(o => o.text.trim()) : null});
    if (++n >= maxItems) break;
  }
  return out;
}
"""

CHALLENGE_RE = re.compile(r"verify (that )?you are (a )?human|are you a robot|unusual traffic|complete the security check|press (and|&) hold", re.I)
CHALLENGE_FRAMES = ("recaptcha", "hcaptcha", "challenges.cloudflare.com", "turnstile", "arkoselabs", "funcaptcha")
RISKY = re.compile(r"\b(submit|apply( now)?|send|purchase|buy|pay|place order|checkout|confirm|publish|post|delete|remove|connect|follow)\b", re.I)


class BrowserManager:
    def __init__(self, home: Path):
        self.home = home
        self._pw = None
        self._contexts: dict[str, Any] = {}
        self._pages: dict[str, Any] = {}
        self._lock = asyncio.Lock()
        self.dialogs: list[dict] = []
        self.allow: dict[str, list[str] | None] = {}   # profile -> host allowlist (None = interactive, unrestricted)

    async def page(self, profile: str = "default", *, headless: bool = True, allow_hosts: list[str] | None = None):
        async with self._lock:
            if self._pw is None:
                from playwright.async_api import async_playwright
                self._pw = await async_playwright().start()
            if profile not in self._contexts:
                udir = self.home / "browser" / profile
                udir.mkdir(parents=True, exist_ok=True)
                ctx = await self._pw.chromium.launch_persistent_context(
                    str(udir), headless=headless, accept_downloads=True, viewport={"width": 1366, "height": 900},
                    downloads_path=str(self.home / "artifacts" / "downloads"))
                await ctx.route("**/*", lambda route, req, p=profile: self._guard(route, req, p))
                self._contexts[profile] = ctx
                page = ctx.pages[0] if ctx.pages else await ctx.new_page()
                page.on("dialog", lambda d: asyncio.ensure_future(self._dialog(d)))
                self._pages[profile] = page
            self.allow[profile] = allow_hosts
            return self._pages[profile]

    async def _guard(self, route, request, profile: str):
        allow = self.allow.get(profile)
        if allow is not None and request.is_navigation_request() and request.frame.parent_frame is None:
            if not host_allowed(request.url, allow):
                await route.abort("blockedbyclient")
                return
        await route.continue_()

    async def _dialog(self, d) -> None:
        self.dialogs.append({"type": d.type, "message": d.message[:300], "ts": time.time()})
        if d.type == "alert":
            await d.accept()
        else:
            await d.dismiss()

    async def check_challenge(self, page) -> dict:
        frames = [f.url for f in page.frames]
        if any(any(k in u for k in CHALLENGE_FRAMES) for u in frames):
            raise NeedsUser(f"{page.url} shows a CAPTCHA / bot challenge. Forge does not bypass these; please complete it or use an official API.")
        try:
            body = (await page.inner_text("body", timeout=3000))[:5000]
        except Exception:
            body = ""
        if CHALLENGE_RE.search(body):
            raise NeedsUser(f"{page.url} asks to verify you are human. Forge does not bypass this.")
        login = await page.locator("input[type=password]").count() > 0
        return {"login_required": login}

    async def open(self, url: str, profile: str, *, headless: bool, allow_hosts: list[str] | None) -> dict:
        page = await self.page(profile, headless=headless, allow_hosts=allow_hosts)
        try:
            resp = await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        except Exception as e:
            if "ERR_BLOCKED_BY_CLIENT" in str(e):
                raise PermissionDenied(f"navigation to {url} blocked: host not in this workflow's allowlist") from None
            raise
        info = await self.check_challenge(page)
        return {"url": page.url, "title": await page.title(), "status": resp.status if resp else None, **info}

    async def snapshot(self, profile: str, max_items: int = 150, text_chars: int = 4000) -> dict:
        page = self._pages.get(profile)
        if page is None:
            raise InterfaceChanged("no open page; call browser.open first")
        els = await page.evaluate(SNAPSHOT_JS, max_items)
        try:
            text = (await page.inner_text("body", timeout=5000)).strip()
        except Exception:
            text = ""
        for e in els:
            e["risky"] = bool(RISKY.search(e["name"] or "")) or (e["type"] == "submit")
        return {"url": page.url, "title": await page.title(), "elements": els, "text": re.sub(r"\n\s*\n+", "\n", text)[:text_chars],
                "dialogs": self.dialogs[-5:]}

    def _loc(self, profile: str, element_id: str):
        page = self._pages.get(profile)
        if page is None:
            raise InterfaceChanged("no open page")
        return page, page.locator(f"[data-forge-id='{element_id}']")

    async def act(self, profile: str, element_id: str, action: str, value: str | None = None, *, allow_risky: bool = False) -> dict:
        page, loc = self._loc(profile, element_id)
        if await loc.count() == 0:
            raise InterfaceChanged(f"element {element_id} is gone (page changed); take a new snapshot")
        name = (await loc.first.evaluate("e => (e.getAttribute('aria-label') || e.innerText || e.value || '').trim()"))[:120]
        typ = (await loc.first.get_attribute("type") or "").lower()
        if action == "click" and not allow_risky and (RISKY.search(name) or typ == "submit"):
            raise PermissionDenied(f"'{name}' looks like a submit/send/purchase control; use browser.submit (requires approval)")
        before = page.url
        if action == "click":
            await loc.first.click(timeout=10000)
        elif action == "fill":
            await loc.first.fill(value or "", timeout=10000)
            got = await loc.first.input_value()
            if got != (value or ""):
                return {"ok": False, "detail": f"field holds {got!r} after fill"}
        elif action == "select":
            await loc.first.select_option(label=value, timeout=10000)
        elif action == "check":
            await loc.first.check(timeout=10000)
        elif action == "uncheck":
            await loc.first.uncheck(timeout=10000)
        elif action == "upload":
            await loc.first.set_input_files(value, timeout=10000)
        else:
            raise PermissionDenied(f"unknown browser action {action}")
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=8000)
        except Exception:
            pass
        info = await self.check_challenge(page)
        return {"ok": True, "url": page.url, "navigated": page.url != before, "title": await page.title(), **info}

    async def download(self, profile: str, element_id: str, dest_dir: Path) -> dict:
        page, loc = self._loc(profile, element_id)
        async with page.expect_download(timeout=60000) as d:
            await loc.first.click()
        dl = await d.value
        dest_dir.mkdir(parents=True, exist_ok=True)
        path = dest_dir / dl.suggested_filename
        await dl.save_as(path)
        return {"path": str(path), "url": dl.url}

    async def screenshot(self, profile: str, dest_dir: Path) -> str:
        page = self._pages[profile]
        dest_dir.mkdir(parents=True, exist_ok=True)
        p = dest_dir / f"web_{int(time.time() * 1000)}.png"
        await page.screenshot(path=str(p), full_page=False)
        return str(p)

    async def close(self) -> None:
        for c in self._contexts.values():
            await c.close()
        if self._pw:
            await self._pw.stop()
        self._contexts.clear()
        self._pages.clear()
        self._pw = None
