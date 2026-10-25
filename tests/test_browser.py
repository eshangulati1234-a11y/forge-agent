import http.server
import threading

import pytest

from forge.errors import NeedsApproval, NeedsUser, PermissionDenied
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
    "/captcha": b"<html><title>Check</title><body>Please verify you are human to continue</body></html>",
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
