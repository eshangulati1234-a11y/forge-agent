import asyncio
import http.server
import os
import threading

import pytest

from forge.errors import InterfaceChanged, NeedsApproval, NeedsUser, PermissionDenied
from forge.policy.engine import Permissions
from forge.tools.invoke import invoke_tool
from forge.tools.registry import ToolContext

PAGES = {
    "/form": b"""<html><head><title>Apply</title></head><body>
      <form action="/done" method="get"><label for=n>Full name</label><input id=n name=n>
      <label for=e>Email</label><input id=e name=e type=email>
      <button type=submit>Submit application</button></form><a href="/other">Other page</a>
      <a href="https://evil.example.org/x">offsite</a></body></html>""",
    "/done": b"<html><title>Thanks</title><body>Application received</body></html>",
    "/jobs": b"""<html><head><title>Jobs</title></head><body><nav>Home Jobs Messaging</nav>
      <main><h1>Machine Learning Engineer</h1><p>We offer visa sponsorship.</p>
      <a href="/jobs/view/111/?trk=a">ML Engineer</a><a href="/jobs/view/111/?trk=b">dup</a><a href="/about">About</a></main>
      <footer>Copyright footer</footer></body></html>""",
    "/late": b"""<html><head><title>Late</title></head><body><main><h1>ML Engineer</h1><div id=d></div></main><script>
      setTimeout(() => { document.getElementById('d').innerText = 'About the job. We sponsor visas.'; }, 1500);
      </script></body></html>""",
    "/silent": b"""<html><title>Job</title><body><main>Job text</main>
      <iframe src="/recaptcha/enterprise/anchor" style="width:0;height:0;border:0;visibility:hidden"></iframe></body></html>""",
    "/visible": b"""<html><title>Job</title><body><main>Job text</main>
      <iframe src="/recaptcha/api2/bframe" width="400" height="580"></iframe></body></html>""",
    "/captcha": b"<html><title>Check</title><body>Please verify you are human to continue</body></html>",
    "/controls": b"""<html><title>Controls</title><body><div style="height:1200px">Scroll test</div>
      <label for=c>Accept updates</label><input id=c type=checkbox>
      <label for=s>Location</label><select id=s><option>Sydney</option><option>Melbourne</option></select>
      <label for=f>Resume</label><input id=f type=file style="display:none">
      <a href="/file" download="example.txt">Download example</a></body></html>""",
    "/file": b"Local download test",
    "/tabs": b"""<html><title>Listing</title><body><a href="/form" target="_blank">Open company careers page</a></body></html>""",
    "/long": b"""<html><title>Long</title><body><div style="height:4000px">top</div><button>Load more</button></body></html>""",
    "/pane": b"""<html><title>Pane</title><body style="margin:0;overflow:hidden">
      <div id=p style="height:100vh;overflow-y:auto"><div style="height:3000px">results</div></div></body></html>""",
}


class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = PAGES.get(self.path.split("?")[0], b"<html><title>404</title></html>")
        self.send_response(200)
        self.send_header("content-type", "text/html")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@pytest.fixture
def site():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


@pytest.fixture(autouse=True)
def hidden_test_browser(services):
    # CI tests should not open windows; the product default is tested separately.
    services.cfg.browser.headless = True
    services.cfg.browser.action_delay_ms = 0


async def test_browser_fill_refuse_submit_and_challenge(services, site):
    import forge.browser.tools as bt
    perms = Permissions(tools=["browser.*"], net=["127.0.0.1"], browser=True)
    ctx = ToolContext(services, run_id="run_b", workflow_id="wf", step_path="s", extra={"permissions": perms})
    call = lambda name, args: invoke_tool(services, name, args, perms, ctx, interactive=False)
    try:
        info = await call("browser.open", {"url": f"{site}/form"})
        assert info["title"] == "Apply" and not info["login_required"]
        snap = await call("browser.snapshot", {})
        by_name = {e["name"]: e for e in snap["elements"]}
        await call("browser.act", {"element_id": by_name["Full name"]["id"], "action": "fill", "value": "Ada Lovelace"})
        btn = by_name["Submit application"]
        assert btn["risky"]
        with pytest.raises(PermissionDenied, match="browser.submit"):
            await call("browser.act", {"element_id": btn["id"], "action": "click"})
        with pytest.raises(NeedsApproval) as e:
            await call("browser.submit", {"element_id": btn["id"], "description": "test application"})
        services.approvals.decide(e.value.approval_id, True)
        res = await call("browser.submit", {"element_id": btn["id"], "description": "test application"})
        assert "Application received" in res["page_text_after"]
        with pytest.raises(PermissionDenied):     # host outside the workflow allowlist
            await call("browser.open", {"url": "https://evil.example.org/x"})
        with pytest.raises(NeedsUser):
            await call("browser.open", {"url": f"{site}/captcha"})
    finally:
        if bt._MGR:
            await bt._MGR.close()
            bt._MGR = None


async def test_browser_follow_up_calls_stay_on_opened_profile(services, site):
    # Regression: an 8B model opened with profile="linkedin", then omitted the profile on find/snapshot/act,
    # which looked in the empty "default" profile and failed with "no open page".
    import forge.browser.tools as bt
    perms = Permissions(tools=["browser.*"], net=["127.0.0.1"], browser=True)
    ctx = ToolContext(services, run_id="run_p", workflow_id="wf", step_path="s", extra={"permissions": perms})
    call = lambda name, args: invoke_tool(services, name, args, perms, ctx, interactive=False)
    try:
        await call("browser.open", {"url": f"{site}/form", "profile": "jobs"})
        snap = await call("browser.snapshot", {})
        assert snap["title"] == "Apply"
        name = next(e for e in snap["elements"] if e["name"] == "Full name")
        assert (await call("browser.act", {"element_id": name["id"], "action": "fill", "value": "Ada"}))["ok"]
        await call("browser.open", {"url": f"{site}/done"})          # no profile: reuses "jobs", not "default"
        assert set(bt._MGR._contexts) == {"jobs"}
    finally:
        if bt._MGR:
            await bt._MGR.close()
            bt._MGR = None


async def test_browser_read_main_text_and_links(services, site):
    import forge.browser.tools as bt
    perms = Permissions(tools=["browser.*"], net=["127.0.0.1"], browser=True)
    ctx = ToolContext(services, run_id="run_r", workflow_id="wf", step_path="s", extra={"permissions": perms})
    call = lambda name, args: invoke_tool(services, name, args, perms, ctx, interactive=False)
    try:
        await call("browser.open", {"url": f"{site}/jobs", "profile": "r"})
        out = await call("browser.read", {"links": r"/jobs/view/\d+"})
        assert "visa sponsorship" in out["text"] and "Messaging" not in out["text"] and "footer" not in out["text"]
        assert [l["clean"] for l in out["links"]] == [f"{site}/jobs/view/111/"]      # deduped, tracking params dropped
        miss = await call("browser.read", {"selector": "#nope"})
        assert "matched nothing" in miss["note"] and "Machine Learning Engineer" in miss["text"]
    finally:
        if bt._MGR:
            await bt._MGR.close()
            bt._MGR = None


async def test_browser_read_waits_for_late_content(services, site):
    # LinkedIn renders the job header first and the description 1-4 s later; reading at load time missed it.
    import forge.browser.tools as bt
    perms = Permissions(tools=["browser.*"], net=["127.0.0.1"], browser=True)
    ctx = ToolContext(services, run_id="run_l", workflow_id="wf", step_path="s", extra={"permissions": perms})
    call = lambda name, args: invoke_tool(services, name, args, perms, ctx, interactive=False)
    try:
        await call("browser.open", {"url": f"{site}/late", "profile": "l"})
        out = await call("browser.read", {"wait_for": "About the job"})
        assert "We sponsor visas" in out["text"] and "note" not in out
        missing = await call("browser.read", {"wait_for": "never appears", "wait_s": 1})
        assert "did not appear" in missing["note"]
    finally:
        if bt._MGR:
            await bt._MGR.close()
            bt._MGR = None


async def test_invisible_recaptcha_is_not_a_challenge(services, site):
    # LinkedIn embeds an invisible reCAPTCHA Enterprise anchor on some job pages; it paused a live run 3 times.
    import forge.browser.tools as bt
    perms = Permissions(tools=["browser.*"], net=["127.0.0.1"], browser=True)
    ctx = ToolContext(services, run_id="run_c", workflow_id="wf", step_path="s", extra={"permissions": perms})
    call = lambda name, args: invoke_tool(services, name, args, perms, ctx, interactive=False)
    try:
        assert (await call("browser.open", {"url": f"{site}/silent", "profile": "c"}))["title"] == "Job"
        with pytest.raises(NeedsUser):
            await call("browser.open", {"url": f"{site}/visible"})
    finally:
        if bt._MGR:
            await bt._MGR.close()
            bt._MGR = None


async def test_launch_mode_override_preserves_existing_session(services, site):
    import forge.browser.tools as bt
    manager = bt.mgr(ToolContext(services))
    try:
        await manager.open(f"{site}/form", "jobs", headless=True, allow_hosts=None)
        page = manager._pages["jobs"]
        await page.locator('#n').fill("Unfinished application")
        with pytest.raises(NeedsUser, match="Restart Forge"):
            await manager.open(f"{site}/done", "jobs", headless=False, allow_hosts=None)
        assert await page.locator('#n').input_value() == "Unfinished application"
        await manager.open(f"{site}/form", "jobs", allow_hosts=None)
        assert manager._pages["jobs"] is page and manager._headless["jobs"] is True
    finally:
        await manager.close()
        bt._MGR = None


async def test_cursor_navigation_actions_and_fallback(services, site, tmp_path):
    from forge.browser.agent import BrowserManager
    from forge.browser.visualize import CURSOR_JS
    headed = os.environ.get("FORGE_BROWSER_HEADED_TEST") == "1"
    manager = BrowserManager(services.cfg.home, headless=not headed, action_delay_ms=300 if headed else 0)
    try:
        page = await manager.page(allow_hosts=None)
        # Exercise the real DOM overlay in a hidden browser to keep CI non-interactive.
        if not headed:
            await page.context.add_init_script(script=CURSOR_JS)
            manager._headless["default"] = False
        await page.goto(f"{site}/form")
        assert await page.locator('[data-forge-overlay]').count() == 1
        snap = await manager.snapshot(None)
        assert "Forge" not in snap["text"]
        assert not any("Forge" in e["name"] for e in snap["elements"])
        field = next(e for e in snap["elements"] if e["name"] == "Full name")
        await manager.act(None, field["id"], "fill", "Ada")
        assert await page.locator('#n').input_value() == "Ada"
        if headed:
            shot = tmp_path / "browser-live.png"
            await page.screenshot(path=str(shot))
            print(f"Live browser screenshot: {shot}")
        assert await page.evaluate("getComputedStyle(document.querySelector('[data-forge-overlay]')).pointerEvents") == "none"
        assert await page.evaluate("document.querySelector('[data-forge-overlay]').shadowRoot.querySelector('#target').style.display") == "block"
        button = next(e for e in snap["elements"] if e["name"] == "Submit application")
        with pytest.raises(PermissionDenied):
            await manager.act(None, button["id"], "click")
        assert await page.evaluate("document.querySelector('[data-forge-overlay]').shadowRoot.querySelector('#pulse').style.display") == ""
        await manager.act(None, button["id"], "click", allow_risky=True)
        assert "Application received" in (await manager.read(None))["text"]
        assert await page.locator('[data-forge-overlay]').count() == 1
        popup = await page.context.new_page()
        await popup.goto(f"{site}/form")
        assert await popup.locator('[data-forge-overlay]').count() == 1
        await popup.close()
        await page.goto(f"{site}/form")
        snap = await manager.snapshot(None)
        field = next(e for e in snap["elements"] if e["name"] == "Full name")
        # A page preventing decoration must not prevent the actual form action.
        await page.evaluate("() => { window.__forgeVisualize = () => { throw Error('overlay unavailable'); }; }")
        await manager.act(None, field["id"], "fill", "Grace")
        assert await page.locator('#n').input_value() == "Grace"
    finally:
        await manager.close()


async def test_visualized_scroll_controls_upload_and_download(services, site, tmp_path):
    from forge.browser.agent import BrowserManager
    from forge.browser.visualize import CURSOR_JS
    manager = BrowserManager(services.cfg.home, headless=True, action_delay_ms=0)
    try:
        page = await manager.page(allow_hosts=None)
        await page.context.add_init_script(script=CURSOR_JS)
        manager._headless["default"] = False
        await page.goto(f"{site}/controls")
        snap = await manager.snapshot(None)
        checkbox = next(e for e in snap["elements"] if e["name"] == "Accept updates")
        await manager.act(None, checkbox["id"], "check")
        assert await page.locator('#c').is_checked()
        assert await page.evaluate("scrollY") > 0
        assert await page.evaluate("document.querySelector('[data-forge-overlay]').shadowRoot.querySelector('#pulse').style.display") == "block"
        await manager.act(None, checkbox["id"], "uncheck")
        assert not await page.locator('#c').is_checked()
        select = next(e for e in snap["elements"] if e["name"] == "Location")
        await manager.act(None, select["id"], "select", "Melbourne")
        assert await page.locator('#s').input_value() == "Melbourne"
        upload = tmp_path / "resume.txt"
        upload.write_text("Local upload test", encoding="utf-8")
        # Hidden file controls are used by real sites and must not require a visual target.
        await page.locator('#f').evaluate("el => el.dataset.forgeId = 'hidden_upload'")
        await manager.act(None, "hidden_upload", "upload", str(upload))
        assert await page.locator('#f').evaluate("el => el.files[0].name") == "resume.txt"
        link = next(e for e in snap["elements"] if e["name"] == "Download example")
        result = await manager.download(None, link["id"], tmp_path)
        from pathlib import Path
        assert Path(result["path"]).read_text() == "Local download test"
    finally:
        await manager.close()


def test_reveal_targets_only_the_profile_browser(monkeypatch, tmp_path):
    from types import SimpleNamespace
    import psutil
    from forge.browser import window
    udir = tmp_path / "browser" / "linkedin"
    procs = [SimpleNamespace(pid=1, info={"cmdline": ["brave.exe", f"--user-data-dir={udir}"]}),
             SimpleNamespace(pid=2, info={"cmdline": ["brave.exe", f'--user-data-dir="{str(udir).upper()}\\"']}),
             SimpleNamespace(pid=3, info={"cmdline": ["brave.exe", r"--user-data-dir=C:\Users\me\Brave"]}),   # user's own
             SimpleNamespace(pid=4, info={"cmdline": None})]
    monkeypatch.setattr(psutil, "process_iter", lambda attrs: procs)
    assert window.profile_pids(udir) == {1, 2}
    assert window.reveal(set()) == 0


@pytest.mark.parametrize("headless", [False, True])
async def test_visible_browser_follows_window_size(services, headless):
    """Headed: no emulated viewport (the page resizes with the window). Headless: the fixed viewport."""
    from types import SimpleNamespace
    from forge.browser.agent import VIEWPORT, BrowserManager
    seen = {}

    async def launch(udir, **kw):
        seen.update(kw)
        raise RuntimeError("stop after capturing launch options")

    manager = BrowserManager(services.cfg.home, headless=headless)
    manager._pw = SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=launch))
    with pytest.raises(RuntimeError, match="stop after"):
        await manager.page("jobs", allow_hosts=None)
    if headless:
        assert seen["viewport"] == VIEWPORT and "no_viewport" not in seen
    else:
        assert seen["no_viewport"] is True and "viewport" not in seen
        assert any(a.startswith("--window-size=") for a in seen["args"])


async def test_links_that_open_tabs_and_switching_tabs(services, site):
    from forge.browser.agent import BrowserManager
    manager = BrowserManager(services.cfg.home, headless=True, action_delay_ms=0)
    try:
        await manager.open(f"{site}/tabs", "jobs", allow_hosts=None)
        link = next(e for e in (await manager.snapshot("jobs"))["elements"] if e["name"] == "Open company careers page")
        await manager.act("jobs", link["id"], "click")
        await asyncio.sleep(0.5)
        snap = await manager.snapshot("jobs")
        assert snap["title"] == "Apply" and snap["tabs"] == 2 and snap["tab"] == 1   # work continues in the new tab
        listed = (await manager.tabs("jobs"))["tabs"]
        assert [t["title"] for t in listed] == ["Listing", "Apply"] and listed[1]["active"]
        await manager.tabs("jobs", switch_to=0)
        assert (await manager.snapshot("jobs"))["title"] == "Listing"
        await manager._contexts["jobs"].pages[0].close()                                 # user closes Forge's tab
        assert (await manager.snapshot("jobs"))["title"] == "Apply"
        with pytest.raises(InterfaceChanged):
            await manager.tabs("jobs", switch_to=5)
    finally:
        await manager.close()


async def test_follows_the_tab_the_user_selects(services, site, monkeypatch):
    from forge.browser import agent as ba
    manager = ba.BrowserManager(services.cfg.home, headless=True, action_delay_ms=0)
    try:
        await manager.open(f"{site}/form", "jobs", allow_hosts=None)
        other = await manager._contexts["jobs"].new_page()
        await other.goto(f"{site}/jobs")
        manager._headless["jobs"], manager._pids["jobs"] = False, {1}   # follow-the-user only runs for visible browsers
        window = {"titles": ["Jobs"]}                                    # the Brave window title names the selected tab
        monkeypatch.setattr(ba, "active_tab_titles", lambda pids: window["titles"])
        assert (await manager._active("jobs")) is other
        window["titles"] = ["Apply"]
        assert await (await manager._active("jobs")).title() == "Apply"
        window["titles"] = []                                            # minimised: keep the current tab
        assert await (await manager._active("jobs")).title() == "Apply"
    finally:
        await manager.close()


async def test_scroll_window_inner_panes_and_to_element(services, site):
    from forge.browser.agent import BrowserManager
    manager = BrowserManager(services.cfg.home, headless=True, action_delay_ms=0)
    try:
        await manager.open(f"{site}/long", "jobs", allow_hosts=None)
        down = await manager.scroll("jobs", "down")
        assert 0 < down["scroll_pct"] < 100 and not down["at_bottom"]
        assert (await manager.scroll("jobs", "bottom"))["at_bottom"]
        assert (await manager.scroll("jobs", "top"))["scroll_pct"] == 0
        button = next(e for e in (await manager.snapshot("jobs"))["elements"] if e["name"] == "Load more")
        assert (await manager.scroll("jobs", element_id=button["id"]))["at_bottom"]
        await manager.open(f"{site}/pane", "jobs", allow_hosts=None)                  # the window can't scroll here
        pane = await manager.scroll("jobs", "down", pages=2)
        assert pane["scroll_pct"] > 0 and await manager._pages["jobs"].evaluate("document.getElementById('p').scrollTop") > 0
    finally:
        await manager.close()
