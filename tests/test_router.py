import httpx
import pytest

from forge.config import ForgeConfig, ModelConfig, NodeConfig
from forge.errors import TransientError
from forge.models.router import FAIL_THRESHOLD, ModelRouter


def cfg(*nodes):
    return ForgeConfig(nodes=[NodeConfig(name=n, url=u) for n, u in nodes],
                       models=[ModelConfig(id="m-big", node=nodes[0][0], capabilities=["reasoning"], quality=3),
                               ModelConfig(id="m-small", node=nodes[-1][0], capabilities=["reasoning", "fast"], quality=1)])


async def test_dead_node_opens_circuit():
    r = ModelRouter(cfg(("dead", "http://127.0.0.1:9")))
    for _ in range(FAIL_THRESHOLD):
        with pytest.raises(TransientError):
            await r.generate("reasoning", [{"role": "user", "content": "hi"}])
    assert not r.nodes["dead"].available()


async def test_fallback_to_second_node():
    r = ModelRouter(cfg(("dead", "http://127.0.0.1:9"), ("live", "http://live")))

    def handler(req: httpx.Request):
        return httpx.Response(200, json={"message": {"content": "ok"}, "eval_count": 1})
    r.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    r.nodes["dead"].healthy = True
    # mark dead node unreachable by making its transport raise
    orig = r.client.post

    async def post(url, **kw):
        if "127.0.0.1:9" in url:
            raise httpx.ConnectError("refused")
        return await orig(url, **kw)
    r.client.post = post
    res = await r.generate("reasoning", [{"role": "user", "content": "hi"}])
    assert res.content == "ok" and res.node == "live"


def test_complexity_low_prefers_small():
    r = ModelRouter(cfg(("a", "http://a"), ("b", "http://b")))
    for n in r.nodes.values():
        n.loaded = {"m-big", "m-small"}
    assert r.candidates("reasoning")[0].id == "m-big"
    assert r.candidates("reasoning", complexity="low")[0].id == "m-small"


@pytest.mark.skipif("not __import__('httpx').get('http://127.0.0.1:11434/api/version').is_success", reason="ollama not running")
async def test_live_ollama_tool_call():
    from forge.config import ForgeConfig
    r = ModelRouter(ForgeConfig(nodes=[NodeConfig(name="blade", url="http://127.0.0.1:11434")],
                                models=[ModelConfig(id="qwen3:8b", node="blade", capabilities=["tool_calling"], ctx=4096)]))
    tools = [{"type": "function", "function": {"name": "get_weather", "description": "weather for a city",
                                               "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}}}]
    res = await r.generate("tool_calling", [{"role": "user", "content": "What's the weather in Melbourne?"}], tools=tools)
    assert res.tool_calls and res.tool_calls[0]["name"] == "get_weather"
    assert "melbourne" in str(res.tool_calls[0]["arguments"]).lower()
