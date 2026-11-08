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
import logging
import re
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from forge.errors import InterfaceChanged, InvalidInput, NeedsUser, PermissionDenied
from forge.policy.engine import host_allowed
from forge.browser.visualize import CURSOR_JS
from forge.browser.window import active_tab_titles, profile_pids, reveal

log = logging.getLogger(__name__)

SNAPSHOT_JS = r"""
(maxItems) => {
  const sel = 'a[href],button,input,select,textarea,[role=button],[role=link],[role=checkbox],[role=radio],[role=tab],[role=menuitem],[role=option],[contenteditable=true],summary';
  const out = [];
  let n = 0;
  for (const el of document.querySelectorAll(sel)) {
    if (el.closest('[data-forge-overlay]')) continue;
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

# Main-content text: the given selector, else the largest main/article, else body; page chrome is hidden (not
# removed, so innerText keeps its line layout) while reading and restored after.
READ_JS = r"""
([sel, linkRe]) => {
  const CHROME = 'script,style,noscript,svg,nav,header,footer,aside,[role=navigation],[role=banner],' +
                 '[role=contentinfo],[role=complementary],[aria-hidden=true]';
  let root = sel ? document.querySelector(sel) : null;
  const matched = !!root;
  if (!root) {
    const cands = [...document.querySelectorAll('main,article,[role=main]')];
    root = cands.sort((a, b) => b.innerText.length - a.innerText.length)[0] || document.body;
  }
  const hidden = [...root.querySelectorAll(CHROME)].map(e => [e, e.style.display]);
  hidden.forEach(([e]) => e.style.display = 'none');
  const text = root.innerText;
  hidden.forEach(([e, d]) => e.style.display = d);
  let links = null;
  if (linkRe) {
    const re = new RegExp(linkRe), seen = new Set();
    links = [];
    for (const a of document.querySelectorAll('a[href]')) {
      if (!re.test(a.href)) continue;
      const u = new URL(a.href), clean = u.origin + u.pathname;
      if (seen.has(clean)) continue;
      seen.add(clean);
      links.push({text: (a.innerText || a.getAttribute('aria-label') || '').trim().slice(0, 160), href: a.href, clean});
    }
  }
  return {text, root: matched ? sel : root.tagName.toLowerCase(), selector_matched: matched, links};
}
"""
SCROLL_JS = r"""
() => {
  window.scrollTo(0, document.body.scrollHeight);
  for (const e of document.querySelectorAll('*')) {
    const s = getComputedStyle(e);
    if ((s.overflowY === 'auto' || s.overflowY === 'scroll') && e.scrollHeight > e.clientHeight + 50) e.scrollTop = e.scrollHeight;
  }
}
"""
SCROLL_TOP_JS = r"""
() => {
  window.scrollTo(0, 0);
  for (const e of document.querySelectorAll('*')) if (e.scrollTop > 0) e.scrollTop = 0;
}
"""
# Where the page is: the window, or the largest scrolled inner pane when the window itself doesn't scroll.
SCROLL_STATE_JS = r"""
() => {
  let el = document.scrollingElement || document.documentElement;
  if (el.scrollHeight <= innerHeight + 2) {
    const panes = [...document.querySelectorAll('*')].filter(e => e.scrollHeight > e.clientHeight + 50 &&
      ['auto', 'scroll'].includes(getComputedStyle(e).overflowY));
    if (panes.length) el = panes.sort((a, b) => b.clientHeight - a.clientHeight)[0];
  }
  const h = el === document.scrollingElement || el === document.documentElement ? innerHeight : el.clientHeight;
  const max = Math.max(0, el.scrollHeight - h);
  return {scroll_pct: max ? Math.round(100 * el.scrollTop / max) : 100, at_bottom: el.scrollTop >= max - 4};
}
"""


def _bare(url: str) -> str:
    """URL without query/fragment/trailing slash, for 'is this the same page' checks."""
    return re.split(r"[?#]", url, maxsplit=1)[0].rstrip("/")


CHALLENGE_FRAMES_JS = r"""
(keys) => [...document.querySelectorAll('iframe')]
  .filter(f => keys.some(k => (f.src || '').includes(k)))
  .map(f => { const r = f.getBoundingClientRect(), s = getComputedStyle(f);
              const shown = s.visibility !== 'hidden' && s.display !== 'none' && parseFloat(s.opacity || '1') > 0.05;
              return {w: shown ? r.width : 0, h: shown ? r.height : 0}; })
"""
CHALLENGE_RE = re.compile(r"verify (that )?you are (a )?human|are you a robot|unusual traffic|complete the security check|press (and|&) hold", re.I)
CHALLENGE_FRAMES = ("recaptcha", "hcaptcha", "challenges.cloudflare.com", "turnstile", "arkoselabs", "funcaptcha")
VIEWPORT = {"width": 1366, "height": 900}
RISKY = re.compile(r"\b(submit|apply( now)?|send|purchase|buy|pay|place order|checkout|confirm|publish|post|delete|remove|connect|follow)\b", re.I)


class BrowserManager:
    def __init__(self, home: Path, executable: str = "", *, headless: bool = False,
                 show_cursor: bool = True, action_delay_ms: int = 300):
        self.home = home
        self.executable = executable or None   # None = Playwright's bundled Chromium
        self.headless = headless
        self.show_cursor = show_cursor
        self.action_delay_ms = action_delay_ms
        self._headless: dict[str, bool] = {}
        self._pw = None
        self._contexts: dict[str, Any] = {}
        self._pages: dict[str, Any] = {}
        self._lock = asyncio.Lock()
        self.dialogs: list[dict] = []
        self.allow: dict[str, list[str] | None] = {}   # profile -> host allowlist (None = interactive, unrestricted)
        self.current: str | None = None                # last opened profile; used when a call omits the profile
        self._pids: dict[str, set[int]] = {}           # profile -> browser process ids (to un-hide its windows)

    def resolve(self, profile: str | None) -> str:
        # Small models open with profile="x" and then drop it on follow-up calls; stay on the page they opened.
        return profile or self.current or "default"

    async def page(self, profile: str | None = None, *, headless: bool | None = None, allow_hosts: list[str] | None = None):
        profile = self.resolve(profile)
        self.current = profile
        async with self._lock:
            if profile in self._contexts and headless is not None and headless != self._headless[profile]:
                raise NeedsUser(f"Browser profile '{profile}' is already running "
                                f"{'hidden' if self._headless[profile] else 'visible'}. Restart Forge to change "
                                "its launch mode; the current browser was left intact to preserve your work.")
            if self._pw is None:
                from playwright.async_api import async_playwright
                self._pw = await async_playwright().start()
            if profile not in self._contexts:
                actual_headless = self.headless if headless is None else headless
                udir = self.home / "browser" / profile
                udir.mkdir(parents=True, exist_ok=True)
                # A visible window gets no emulated viewport: the page follows the real window, so the user can resize
                # or maximise it. (A fixed viewport clipped pages in a smaller window and ignored resizing.)
                size = {"viewport": VIEWPORT} if actual_headless else \
                    {"no_viewport": True, "args": [f"--window-size={VIEWPORT['width'] + 34},{VIEWPORT['height'] + 150}"]}
                ctx = await self._pw.chromium.launch_persistent_context(
                    str(udir), executable_path=self.executable, headless=actual_headless, accept_downloads=True,
                    downloads_path=str(self.home / "artifacts" / "downloads"), **size)
                await ctx.route("**/*", lambda route, req, p=profile: self._guard(route, req, p))
                self._contexts[profile] = ctx
                self._headless[profile] = actual_headless
                page = ctx.pages[0] if ctx.pages else await ctx.new_page()
                if self.show_cursor and not actual_headless:
                    try:
                        await ctx.add_init_script(script=CURSOR_JS)
                        for existing_page in ctx.pages:
                            await existing_page.evaluate(CURSOR_JS)
                    except Exception:
                        log.debug("Could not install browser cursor", exc_info=True)
                page.on("dialog", lambda d: asyncio.ensure_future(self._dialog(d)))
                ctx.on("page", lambda p, prof=profile: self._adopt(prof, p))
                self._pages[profile] = page
            if not self._headless[profile] and sys.platform == "win32":
                await self._reveal(profile)
            self.allow[profile] = allow_hosts
        return await self._active(profile)

    def _adopt(self, profile: str, page) -> None:
        """A tab opened by a link or by the user is where the work continues, as it would be for a person."""
        page.on("dialog", lambda d: asyncio.ensure_future(self._dialog(d)))
        self._pages[profile] = page

    async def _active(self, profile: str | None):
        """The tab to work on: the one the user has selected in the window (visible), else the last one Forge used.
        The browser is shared with the user, who may switch tabs or close the one Forge opened."""
        profile = self.resolve(profile)
        ctx, cur = self._contexts.get(profile), self._pages.get(profile)
        if ctx is None:
            return cur
        pages = [x for x in ctx.pages if not x.is_closed()]
        if cur is None or cur.is_closed():
            cur = pages[-1] if pages else None
        if len(pages) > 1 and not self._headless.get(profile, self.headless) and self._pids.get(profile):
            shown = set(await asyncio.to_thread(active_tab_titles, self._pids[profile]))
            if shown:   # none when minimised or hidden: keep the current tab
                titled = []
                for x in pages:
                    try:
                        titled.append((await asyncio.wait_for(x.title(), 1.5), x))
                    except Exception:   # a tab mid-navigation can't answer; it isn't the one to switch to
                        pass
                selected = [x for t, x in titled if t in shown]
                if selected and cur not in selected:
                    cur = selected[0]
        self._pages[profile] = cur
        return cur

    async def _require(self, profile: str | None):
        page = await self._active(profile)
        if page is None:
            raise InterfaceChanged("no open page; call browser.open first")
        return page

    async def tabs(self, profile: str | None, switch_to: int | None = None) -> dict:
        """List the profile's tabs; optionally bring tab `switch_to` to the front and work there."""
        profile = self.resolve(profile)
        cur = await self._require(profile)
        pages = [x for x in self._contexts[profile].pages if not x.is_closed()]
        if switch_to is not None:
            if not 0 <= switch_to < len(pages):
                raise InterfaceChanged(f"no tab {switch_to}; there are {len(pages)} tabs (0-{len(pages) - 1})")
            cur = pages[switch_to]
            await cur.bring_to_front()
            self._pages[profile] = cur
        out = []
        for i, x in enumerate(pages):
            try:
                title = await asyncio.wait_for(x.title(), 1.5)
            except Exception:
                title = ""
            out.append({"index": i, "title": title, "url": x.url, "active": x is cur})
        return {"tabs": out}

    async def scroll(self, profile: str | None, direction: str = "down", pages: float = 1.0, element_id: str | None = None) -> dict:
        """Scroll like a person: the mouse wheel over the middle of the page moves whatever pane is under it (many
        sites scroll an inner panel, not the window). top/bottom jump; element_id brings that element into view."""
        page = await self._require(profile)
        if element_id:
            loc = page.locator(f"[data-forge-id='{element_id}']")
            if await loc.count() == 0:
                raise InterfaceChanged(f"element {element_id} is gone (page changed); take a new snapshot")
            await loc.first.scroll_into_view_if_needed(timeout=5000)
        elif direction in ("top", "bottom"):
            await page.evaluate(SCROLL_JS if direction == "bottom" else SCROLL_TOP_JS)
        elif direction in ("down", "up"):
            w, h = await page.evaluate("() => [innerWidth, innerHeight]")
            await page.mouse.move(w / 2, h / 2)
            await page.mouse.wheel(0, (1 if direction == "down" else -1) * h * 0.8 * pages)
        else:
            raise InterfaceChanged(f"unknown scroll direction {direction!r}; use down, up, top or bottom")
        await asyncio.sleep(0.4)
        return await page.evaluate(SCROLL_STATE_JS)

    async def _reveal(self, profile: str) -> None:
        """A headed browser can start hidden on Windows (see forge.browser.window); show it so the user can watch."""
        try:
            if not self._pids.get(profile):
                self._pids[profile] = await asyncio.to_thread(profile_pids, self.home / "browser" / profile)
            await asyncio.to_thread(reveal, self._pids[profile])
        except Exception:
            log.debug("could not show the browser window", exc_info=True)

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
        # LinkedIn embeds an invisible reCAPTCHA Enterprise anchor on some job pages (silent scoring, no puzzle); counting
        # every provider frame paused runs on pages the user could read normally. Only a frame big enough to interact
        # with counts. Read from the DOM (src attribute), not page.frames: a just-inserted frame is still about:blank.
        frames = await page.evaluate(CHALLENGE_FRAMES_JS, list(CHALLENGE_FRAMES))
        if any(f["w"] >= 100 and f["h"] >= 60 for f in frames):
            raise NeedsUser(f"{page.url} shows a CAPTCHA / bot challenge. Forge does not bypass these; please complete it or use an official API.")
        try:
            body = (await page.inner_text("body", timeout=3000))[:5000]
        except Exception:
            body = ""
        if CHALLENGE_RE.search(body):
            raise NeedsUser(f"{page.url} asks to verify you are human. Forge does not bypass this.")
        # Many public pages embed hidden sign-in forms. Even a visible optional form is not a login wall.
        login_form = await page.locator("input[type=password]:visible").count() > 0
        path = urlparse(page.url).path
        title = await page.title()
        gate = bool(re.search(r"/(login|signin|sign-in|authwall)(/|$)", path, re.I) or
                    re.search(r"\b(log in|sign in|login)\b", title, re.I) or
                    re.search(r"\b(sign in|log in|login) (is required|to (view|continue|access))\b", body, re.I))
        return {"login_required": login_form and gate, "login_form_present": login_form}

    async def open(self, url: str, profile: str | None, *, headless: bool | None = None,
                   allow_hosts: list[str] | None, new_tab: bool = False) -> dict:
        page = await self.page(profile, headless=headless, allow_hosts=allow_hosts)
        if new_tab:
            page = await self._contexts[self.resolve(profile)].new_page()
            self._pages[self.resolve(profile)] = page
            await page.bring_to_front()
        try:
            resp = await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        except Exception as e:
            if "ERR_BLOCKED_BY_CLIENT" in str(e):
                raise PermissionDenied(f"navigation to {url} blocked: host not in this workflow's allowlist") from None
            raise
        info = await self.check_challenge(page)
        return {"url": page.url, "title": await page.title(), "status": resp.status if resp else None, **info}

    async def snapshot(self, profile: str | None, max_items: int = 150, text_chars: int = 4000) -> dict:
        page = await self._require(profile)
        els = await page.evaluate(SNAPSHOT_JS, max_items)
        try:
            text = (await page.inner_text("body", timeout=5000)).strip()
        except Exception:
            text = ""
        for e in els:
            e["risky"] = bool(RISKY.search(e["name"] or "")) or (e["type"] == "submit")
        pages = [x for x in page.context.pages if not x.is_closed()]
        return {"url": page.url, "title": await page.title(), "elements": els, "text": re.sub(r"\n\s*\n+", "\n", text)[:text_chars],
                "dialogs": self.dialogs[-5:], "tabs": len(pages), "tab": pages.index(page) if page in pages else 0,
                **await page.evaluate(SCROLL_STATE_JS)}

    async def read(self, profile: str | None, *, selector: str | None = None, max_chars: int = 8000,
                   links: str | None = None, scroll: bool = False, wait_for: str | None = None,
                   wait_s: float = 15.0, url: str | None = None, allow_hosts: list[str] | None = None) -> dict:
        page = await self._active(profile)
        if url and (page is None or _bare(page.url) != _bare(url)):
            # A resumed run replays a finished browser.open from storage without navigating, so the page may still
            # show a later job; reading it would file that job's text under this one.
            await self.open(url, profile, allow_hosts=allow_hosts)
            page = await self._active(profile)
        if page is None:
            raise InterfaceChanged("no open page; call browser.open first")
        waited_for = None
        if wait_for:
            try:
                await page.wait_for_function("t => document.body && document.body.innerText.includes(t)", arg=wait_for,
                                             timeout=wait_s * 1000)
                waited_for = True
            except Exception:
                waited_for = False
        await self._settle(page, max_s=min(5.0, wait_s))
        if scroll:
            for _ in range(6):   # lazy lists (LinkedIn's results pane) load ~7 items per scroll
                await page.evaluate(SCROLL_JS)
                await asyncio.sleep(0.6)
        r = await page.evaluate(READ_JS, [selector, links])
        text = re.sub(r"\n\s*\n+", "\n", r["text"] or "").strip()
        out = {"url": page.url, "title": await page.title(), "text": text[:max_chars], "truncated": len(text) > max_chars,
               "read_from": r["root"], **await self.check_challenge(page)}
        if selector and not r["selector_matched"]:
            out["note"] = f"selector {selector!r} matched nothing; read {r['root']} instead"
        if waited_for is False:
            out["note"] = (out.get("note", "") + f" text {wait_for!r} did not appear within {wait_s:.0f}s").strip()
        if links is not None:
            out["links"] = r["links"]
        return out

    @staticmethod
    async def _settle(page, max_s: float) -> None:
        """Pages like LinkedIn's job view render the header at domcontentloaded and fetch the body ~1-4 s later (main
        grew from 1.3k to 9.6k chars). Wait until the text length stops changing for a second, up to max_s."""
        last, stable, end = -1, 0, time.time() + max_s
        while time.time() < end:
            n = await page.evaluate("() => document.body ? document.body.innerText.length : 0")
            stable = stable + 1 if n == last else 0
            if stable >= 2:
                return
            last = n
            await asyncio.sleep(0.5)

    async def _loc(self, profile: str | None, element_id: str):
        page = await self._require(profile)
        return page, page.locator(f"[data-forge-id='{element_id}']")

    async def _visualize(self, profile: str | None, loc, *, click: bool = False) -> None:
        """Best-effort decoration; execution and actionability stay with Playwright locators."""
        if self._headless.get(self.resolve(profile), self.headless):
            return
        if self.show_cursor:
            try:
                # Hidden file inputs must remain usable via set_input_files.
                if await loc.is_visible():
                    await loc.scroll_into_view_if_needed(timeout=1500)
                    await loc.evaluate(CURSOR_JS, timeout=1500)
                    await loc.evaluate("(el, args) => window.__forgeVisualize?.(el, args.click, args.delay)",
                                       {"click": click, "delay": self.action_delay_ms}, timeout=1500)
            except Exception:
                log.debug("Could not visualize browser action", exc_info=True)
        await asyncio.sleep(self.action_delay_ms / 1000)

    async def act(self, profile: str, element_id: str, action: str, value: str | None = None, *, allow_risky: bool = False) -> dict:
        page, loc = await self._loc(profile, element_id)
        if await loc.count() == 0:
            raise InterfaceChanged(f"element {element_id} is gone (page changed); take a new snapshot")
        name = (await loc.first.evaluate("e => (e.getAttribute('aria-label') || e.innerText || e.value || '').trim()"))[:120]
        typ = (await loc.first.get_attribute("type") or "").lower()
        if action == "click" and not allow_risky and (RISKY.search(name) or typ == "submit"):
            raise PermissionDenied(f"'{name}' looks like a submit/send/purchase control; use browser.submit (requires approval)")
        before = page.url
        if action not in {"click", "fill", "select", "check", "uncheck", "upload"}:
            raise PermissionDenied(f"unknown browser action {action}")
        await self._visualize(profile, loc.first, click=action in {"click", "check", "uncheck"})
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
        page, loc = await self._loc(profile, element_id)
        await self._visualize(profile, loc.first, click=True)
        before = page.url
        download = asyncio.create_task(page.wait_for_event("download", timeout=60000))
        navigation = asyncio.create_task(page.wait_for_url(lambda url: str(url) != before, timeout=60000))
        try:
            await loc.first.click()
            done, _ = await asyncio.wait({download, navigation}, return_when=asyncio.FIRST_COMPLETED)
            if download not in done:
                await navigation
                raise InvalidInput("This element navigated to a web page instead of downloading a file. "
                                   "Use browser.open with new_tab=true to open links; use browser.read for page contents.")
            dl = await download
        finally:
            for pending in (download, navigation):
                if not pending.done():
                    pending.cancel()
            await asyncio.gather(download, navigation, return_exceptions=True)
        dest_dir.mkdir(parents=True, exist_ok=True)
        path = dest_dir / dl.suggested_filename
        await dl.save_as(path)
        return {"path": str(path), "url": dl.url}

    async def screenshot(self, profile: str | None, dest_dir: Path) -> str:
        page = await self._require(profile)
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
        self._headless.clear()
        self._pids.clear()
        self._pw = None
