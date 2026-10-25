import time

import pytest

from forge.errors import EmergencyStop, NeedsApproval, PermissionDenied
from forge.policy.engine import Permissions
from forge.tools.invoke import invoke_tool
from forge.tools.registry import ToolContext


def ctx(s, **kw):
    return ToolContext(s, run_id=kw.get("run_id", "run_x"), workflow_id="wf", step_path="st")


async def test_default_deny(services):
    with pytest.raises(PermissionDenied):
        await invoke_tool(services, "test.echo", {"value": 1}, Permissions(), ctx(services), interactive=False)


async def test_granted_tool_runs_and_is_recorded(services):
    out = await invoke_tool(services, "test.echo", {"value": 1}, Permissions(tools=["test.*"]), ctx(services), interactive=False)
    assert out == {"value": 1}
    assert services.db.one("SELECT status FROM tool_calls WHERE tool='test.echo'")["status"] == "ok"


async def test_invalid_args_rejected_before_policy(services):
    from forge.errors import InvalidInput
    with pytest.raises(InvalidInput) as e:
        await invoke_tool(services, "test.send", {"to": "x"}, Permissions(tools=["*"]), ctx(services), interactive=False)
    assert any(err["field"] == "body" for err in e.value.details["errors"])


async def test_path_outside_grant_denied(services, tmp_path):
    perms = Permissions(tools=["test.*"], fs_write=[str(tmp_path / "allowed")])
    with pytest.raises(PermissionDenied):
        await invoke_tool(services, "test.count", {"path": str(tmp_path / "elsewhere" / "c.txt")}, perms, ctx(services), interactive=False)
    (tmp_path / "allowed").mkdir()
    out = await invoke_tool(services, "test.count", {"path": str(tmp_path / "allowed" / "c.txt")}, perms, ctx(services), interactive=False)
    assert out["n"] == 1


async def test_protected_path_denied_even_if_granted(services, home):
    perms = Permissions(tools=["*"], fs_write=[str(home)])
    with pytest.raises(PermissionDenied, match="protected"):
        await invoke_tool(services, "fs.write_text", {"path": str(home / "forge.toml"), "text": "x"}, perms, ctx(services), interactive=False)


async def test_external_requires_single_use_signed_approval(services):
    perms = Permissions(tools=["test.*"])
    args = {"to": "a@b.c", "body": "hi"}
    with pytest.raises(NeedsApproval) as e:
        await invoke_tool(services, "test.send", args, perms, ctx(services), interactive=False)
    ap_id = e.value.approval_id
    services.approvals.decide(ap_id, True)
    out = await invoke_tool(services, "test.send", args, perms, ctx(services), interactive=False)
    assert out["sent_to"] == "a@b.c"
    # single use: the same action needs a fresh approval
    with pytest.raises(NeedsApproval):
        await invoke_tool(services, "test.send", args, perms, ctx(services), interactive=False)


async def test_approval_does_not_transfer_to_different_args(services):
    perms = Permissions(tools=["test.*"])
    with pytest.raises(NeedsApproval) as e:
        await invoke_tool(services, "test.send", {"to": "a@b.c", "body": "hi"}, perms, ctx(services), interactive=False)
    services.approvals.decide(e.value.approval_id, True)
    with pytest.raises(NeedsApproval):
        await invoke_tool(services, "test.send", {"to": "evil@x.y", "body": "hi"}, perms, ctx(services), interactive=False)


async def test_tampered_approval_rejected(services):
    perms = Permissions(tools=["test.*"])
    args = {"to": "a@b.c", "body": "hi"}
    with pytest.raises(NeedsApproval) as e:
        await invoke_tool(services, "test.send", args, perms, ctx(services), interactive=False)
    # attacker flips the row directly in the DB without the signing key
    services.db.execute("UPDATE approvals SET status='approved', decided_at=?, decided_by='user', signature='00' WHERE id=?",
                        (time.time(), e.value.approval_id))
    with pytest.raises(NeedsApproval):
        await invoke_tool(services, "test.send", args, perms, ctx(services), interactive=False)
    ok, _ = services.db.verify_audit_chain()
    assert ok


async def test_expired_approval_not_honoured(services):
    ap = services.approvals.request("action", "t", {"x": 1}, "test", ttl=-1)
    with pytest.raises(ValueError, match="expired"):
        services.approvals.decide(ap["id"], True)


async def test_emergency_stop(services):
    services.policy.set_stop(True)
    with pytest.raises(EmergencyStop):
        await invoke_tool(services, "test.echo", {}, Permissions(tools=["*"]), ctx(services), interactive=False)
    services.policy.set_stop(False)


async def test_interactive_net_widening_asks(services):
    from forge.policy.engine import session_permissions
    perms = session_permissions(services.cfg.workspace_path)
    c = ToolContext(services, session_id="sess1")
    with pytest.raises(NeedsApproval):
        await invoke_tool(services, "http.get", {"url": "https://example.com/"}, perms, c, interactive=True)


def test_audit_chain_detects_tamper(services):
    services.db.audit("a", "e1", {"x": 1})
    services.db.audit("a", "e2", {"x": 2})
    assert services.db.verify_audit_chain()[0]
    services.db.execute("UPDATE audit SET data_json='{\"x\":9}' WHERE event='e1'")
    assert not services.db.verify_audit_chain()[0]
