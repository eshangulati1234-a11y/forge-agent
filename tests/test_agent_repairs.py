import asyncio

import httpx
import pytest
from pydantic import BaseModel

from forge.agent.loop import Agent
from forge.config import api_token
from forge.policy.engine import Permissions
from forge.tools.invoke import invoke_tool
from forge.tools.registry import ToolContext, tool
from tests.test_agent import _agent


async def test_interrupt_endpoint_cancels_on_event_loop(services, monkeypatch):
    from forge.daemon import app as daemon
    agent = Agent(services)
    sid = agent.new_session()
    loop = asyncio.get_running_loop()
    original = agent.interrupt

    def interrupt(sid):
        assert asyncio.get_running_loop() is loop
        original(sid)

    monkeypatch.setattr(agent, "interrupt", interrupt)
    monkeypatch.setattr(daemon, "AGENT", agent)
    task = asyncio.create_task(asyncio.Event().wait())
    agent._running[sid] = task
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=daemon.app), base_url="http://test",
                                    headers={"Authorization": f"Bearer {api_token()}"}) as client:
            response = await client.post(f"/sessions/{sid}/interrupt")
            assert response.status_code == 200
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, .5)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_cancelled_tool_is_recorded_and_exits_promptly(services):
    entered = asyncio.Event()

    class Empty(BaseModel):
        pass

    @tool("test.wait_for_interrupt", Empty, registry=services.registry)
    async def wait(a, ctx):
        entered.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(invoke_tool(services, "test.wait_for_interrupt", {}, Permissions(tools=["*"]),
                                           ToolContext(services, session_id="interrupt-test"), interactive=True))
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, .5)
    row = services.db.one("SELECT status,error_class FROM tool_calls WHERE tool='test.wait_for_interrupt'")
    assert row == {"status": "cancelled", "error_class": "cancelled"}


async def test_tool_search_exposes_full_schemas_in_following_model_call(services):
    agent, router = _agent(services, [{"name": "tools__search", "arguments": {"query": "excel"}}])
    await agent.run_turn(agent.new_session(), "find the right capability")
    offered = set(router.offered[1])
    assert {"excel__create", "excel__open", "excel__write_cells"} <= offered
    services.registry.disabled = ["excel.open"]
    assert "excel.open" not in {t.name for t in agent._tools_for(agent.sessions()[0]["id"], "excel spreadsheet")}


def test_spreadsheet_request_gets_creation_tools_without_search(services):
    offered = {t.name for t in Agent(services)._tools_for("new", "find jobs in browser and create an excel file")}
    assert {"excel.create", "excel.open", "excel.write_cells"} <= offered
