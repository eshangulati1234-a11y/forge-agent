"""Agent harness: tool exposure, token accounting, no-progress stop, subagent grants. A scripted router stands in
for the model so these run offline and deterministically."""
from forge.agent.loop import STREAK_NUDGE, STREAK_STOP, Agent, Budget
from forge.config import ModelConfig
from forge.models.router import GenResult


class ScriptedRouter:
    """Returns the scripted tool calls in order, then a final answer; records what the agent offered."""

    def __init__(self, calls: list[dict], prompt_tokens: int = 5000):
        self.calls, self.prompt_tokens, self.offered = list(calls), prompt_tokens, []

    async def stream(self, capability, messages, tools=None, **kw):
        self.offered.append(sorted(t["function"]["name"] for t in tools or []))
        call = self.calls.pop(0) if self.calls else None
        yield {"done": GenResult(content="" if call else "done", tool_calls=[call] if call else [], model="fake", node="x",
                                 prompt_tokens=self.prompt_tokens, completion_tokens=20)}

    async def embed(self, texts):
        raise RuntimeError("no embeddings in tests")

    def candidates(self, capability, *a, **kw):
        return [ModelConfig(id="fake", node="x", capabilities=[capability], ctx=16384)]


def _agent(services, calls, **kw) -> tuple[Agent, ScriptedRouter]:
    router = ScriptedRouter(calls, **kw)
    services.router = router
    services.memory.router = None
    return Agent(services), router


async def test_browser_tools_are_offered_as_a_group_and_desktop_only_when_asked(services):
    agent, router = _agent(services, [])
    await agent.run_turn(agent.new_session(), "open the jobs page in the browser and save a file")
    offered = {n.replace("__", ".") for n in router.offered[0]}
    assert {"browser.open", "browser.read", "browser.snapshot", "browser.find", "browser.act"} <= offered
    assert not any(n.startswith("desktop.") for n in offered)


async def test_token_budget_counts_new_tokens_not_resent_context(services):
    ws = services.cfg.workspace_path
    ws.mkdir(parents=True, exist_ok=True)
    calls = [{"name": "test__count", "arguments": {"path": str(ws / f"t{i}.txt")}} for i in range(14)]   # new target each
    agent, _ = _agent(services, calls, prompt_tokens=5000)
    budget = Budget(max_steps=30)
    final = await agent.run_turn(agent.new_session(), "echo things", budget=budget)
    # Old accounting: 15 steps x 5020 = 75k -> stopped at ~12 steps. Now: 5000 once + 20 per step.
    assert final == "done" and budget.used_tokens == 5000 + 15 * 20


async def test_same_tool_on_same_target_is_nudged_then_stopped(services):
    calls = [{"name": "test__echo", "arguments": {"value": f"wording {i}"}} for i in range(20)]
    agent, _ = _agent(services, calls)
    sid = agent.new_session()
    final = await agent.run_turn(sid, "echo things", budget=Budget(max_steps=30))
    assert final.startswith("Stopped: I called test.echo") and f"{STREAK_STOP} times" in final
    tool_msgs = [m["content"] for m in agent.history(sid) if m["role"] == "tool"]
    assert len(tool_msgs) == STREAK_STOP
    assert "[forge harness]" in tool_msgs[STREAK_NUDGE - 1] and "[forge harness]" not in tool_msgs[0]


async def test_new_target_resets_the_streak(services, tmp_path):
    ws = services.cfg.workspace_path
    ws.mkdir(parents=True, exist_ok=True)
    calls = [{"name": "test__count", "arguments": {"path": str(ws / f"c{i}.txt")}} for i in range(STREAK_STOP + 2)]
    agent, _ = _agent(services, calls)
    final = await agent.run_turn(agent.new_session(), "count", budget=Budget(max_steps=30))
    assert final == "done"


async def test_subagent_shares_root_session_grants_and_gets_the_goal(services):
    agent, _ = _agent(services, [])
    root = agent.new_session()
    agent._save(root, "user", "GOAL: find AI jobs on LinkedIn using profile linkedin")
    seen = {}

    async def fake_turn(sid, task, budget=None, emit=None):
        seen.update(sid=sid, task=task, root=agent._root.get(sid), steps=budget.max_steps)
        return "ok"

    agent.run_turn = fake_turn
    out = await agent._delegate(root, {"task": "read job 1", "max_steps": 5}, lambda *a, **k: None)
    assert out["answer"] == "ok" and seen["root"] == root and seen["steps"] == 5
    assert "GOAL: find AI jobs" in seen["task"]
    assert out["subagent_session"] not in agent._root          # mapping cleaned up


def test_budget_pause_excludes_approval_wait():
    b = Budget(max_seconds=10)
    b.started -= 9
    b.pause(5)
    assert b.exceeded() is None


def test_job_application_on_the_web_gets_no_desktop_tools(services):
    agent = Agent(services)
    web = {t.name for t in agent._tools_for("s", "fill in the job application on jobs.ashbyhq.com with my details")}
    assert "browser.act" in web and not any(n.startswith("desktop.") for n in web)
    desk = {t.name for t in agent._tools_for("s", "open the calculator, then read its window")}
    assert any(n.startswith("desktop.") for n in desk)
