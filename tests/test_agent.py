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


async def _fake_summary(capability, messages, **kw):
    assert capability == "extraction" and "USER: find AI jobs" in messages[1]["content"]
    return GenResult(content="Goal: AI jobs in Melbourne. Saved 3 to linkedin_jobs.", tool_calls=[], model="fake", node="x")


def _chat(agent, sid, turns: int) -> None:
    for i in range(turns):
        agent._save(sid, "user", "find AI jobs" if i == 0 else f"next {i}")
        agent._save(sid, "assistant", "", tool_calls=[{"name": "browser__read", "arguments": {"url": f"u{i}"}}])
        agent._save(sid, "tool", "x" * 3000, tool_name="browser.read")
        agent._save(sid, "assistant", f"answer {i}")


async def test_compact_folds_old_messages_into_a_summary_the_model_sees(services, monkeypatch):
    agent = Agent(services)
    sid = agent.new_session()
    _chat(agent, sid, 10)
    before = agent.context_info(sid)
    assert before["history_messages"] == 40 and before["dropped_messages"] > 0 and before["can_compact"]
    assert before["used"] is None and 5000 < before["estimate"] < 12000 and before["window"] == services.router.candidates("tool_calling")[0].ctx

    monkeypatch.setattr(services.router, "generate", _fake_summary)
    out = await agent.compact(sid)
    assert out["compacted"] == 35 and out["history_tokens_after"] < out["history_tokens_before"]
    after = agent.context_info(sid)
    assert after["history_messages"] == 5 and after["dropped_messages"] == 0 and after["summary"]
    assert after["compacted_messages"] == 35
    assert "Saved 3 to linkedin_jobs" in agent._system(sid)
    assert [m["content"] for m in agent._context(sid, 5000)][0] == "answer 8"   # cut moved past the tool result
    assert len(agent.history(sid)) == 41                                       # nothing deleted; transcript intact


async def test_second_compact_folds_the_previous_summary(services, monkeypatch):
    agent = Agent(services)
    sid = agent.new_session()
    _chat(agent, sid, 4)
    monkeypatch.setattr(services.router, "generate", _fake_summary)
    await agent.compact(sid)
    _chat(agent, sid, 3)
    seen = {}

    async def second(capability, messages, **kw):
        seen["text"] = messages[1]["content"]
        return GenResult(content="merged summary", tool_calls=[], model="fake", node="x")

    monkeypatch.setattr(services.router, "generate", second)
    await agent.compact(sid)
    assert seen["text"].startswith("EARLIER SUMMARY: Goal: AI jobs")
    assert agent._summary(sid) == "merged summary"
    assert services.db.one("SELECT COUNT(*) n FROM messages WHERE session_id=? AND role='summary' AND compacted=0", (sid,))["n"] == 1


async def test_compact_with_short_history_does_nothing(services):
    agent = Agent(services)
    sid = agent.new_session()
    _chat(agent, sid, 1)
    assert (await agent.compact(sid))["compacted"] == 0


async def test_turn_records_context_usage(services):
    agent, _ = _agent(services, [], prompt_tokens=7000)
    sid = agent.new_session()
    await agent.run_turn(sid, "hello")
    info = agent.context_info(sid)
    assert info["model"] == "fake" and info["used"] == 7020 and info["remaining"] == 16384 - 7020


def test_job_application_on_the_web_gets_no_desktop_tools(services):
    agent = Agent(services)
    web = {t.name for t in agent._tools_for("s", "fill in the job application on jobs.ashbyhq.com with my details")}
    assert "browser.act" in web and not any(n.startswith("desktop.") for n in web)
    desk = {t.name for t in agent._tools_for("s", "open the calculator, then read its window")}
    assert any(n.startswith("desktop.") for n in desk)
