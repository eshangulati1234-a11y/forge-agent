import sys
from pathlib import Path

import pytest

from forge.config import McpServerConfig
from forge.errors import InvalidInput, NeedsApproval
from forge.policy.engine import Permissions
from forge.tools.invoke import invoke_tool
from forge.tools.mcp_bridge import McpBridge
from forge.tools.registry import ToolContext


async def test_mcp_tools_bridged_validated_and_gated(services):
    services.cfg.mcp_servers = [McpServerConfig(name="echo", command=sys.executable,
                                                args=[str(Path(__file__).with_name("mcp_echo_server.py"))])]
    bridge = McpBridge(services)
    try:
        added = await bridge.start()
        assert "mcp.echo.shout" in added
        perms = Permissions(tools=["mcp.*"])
        ctx = ToolContext(services, run_id="r1", workflow_id="w", step_path="s")
        with pytest.raises(InvalidInput):                         # server schema enforced before the call
            await invoke_tool(services, "mcp.echo.shout", {"times": 2}, perms, ctx, interactive=False)
        with pytest.raises(NeedsApproval) as e:                   # untrusted MCP tool: approval-gated by default
            await invoke_tool(services, "mcp.echo.shout", {"text": "hi", "times": 2}, perms, ctx, interactive=False)
        services.approvals.decide(e.value.approval_id, True)
        out = await invoke_tool(services, "mcp.echo.shout", {"text": "hi", "times": 2}, perms, ctx, interactive=False)
        assert "HI HI" in " ".join(map(str, out["content"]))
    finally:
        await bridge.aclose()
