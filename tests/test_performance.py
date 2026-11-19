"""Latency fixes: search buttons aren't 'submit' controls, find answers obvious cases without a model, the LLM
fallback prompt stays small, and Ollama may keep several models resident on large-memory machines."""
import pytest

from forge.browser.agent import is_risky_control
from forge.browser.tools import fast_match
from forge.runtime.decisions import LLM_MAX_OPTIONS, Decisions
from forge.setup.hardware import recommended_max_loaded


@pytest.mark.parametrize("name,typ,risky", [
    ("Submit search", "submit", False), ("Search", "submit", False), ("Find jobs", "submit", False),
    ("Filter results", "button", False), ("Submit application", "submit", True), ("Submit", "submit", True),
    ("Apply now", "button", True), ("Search and apply", "submit", True), ("Send message", "submit", True),
    ("Buy now", "button", True), ("Sign up", "button", True), ("Next", "button", False), ("", "submit", True),
])
def test_search_controls_are_safe_but_state_changing_ones_are_not(name, typ, risky):
    assert is_risky_control(name, typ) is risky


ELS = [{"id": "e1", "role": "textbox", "name": "Search jobs"}, {"id": "e2", "role": "button", "name": "Search"},
       {"id": "e3", "role": "link", "name": "Sign in"}, {"id": "e4", "role": "link", "name": "Jobs board"},
       {"id": "e5", "role": "link", "name": "Jobs in Melbourne"}]


def test_fast_match_answers_unambiguous_requests_and_never_guesses():
    assert fast_match(ELS, "the search box")["id"] == "e1"          # role hint: box -> textbox, not the button
    assert fast_match(ELS, "search button")["id"] == "e2"
    assert fast_match(ELS, "sign in link")["id"] == "e3"
    assert fast_match(ELS, "link to jobs") is None                   # two jobs links: ask the models
    assert fast_match(ELS, "the cheese counter") is None             # nothing matches: ask the models
    assert fast_match(ELS, "the box") is None                        # no content words


def test_ollama_residency_scales_with_memory():
    models = [6400, 3200, 6200, 700]
    assert recommended_max_loaded(models, 8192) == 1                 # 8 GB NVIDIA: one model at a time
    assert recommended_max_loaded(models, 21626) == 3                # 32 GB Mac: main + vision + a small one stay loaded
    assert recommended_max_loaded([], 0) == 1


async def test_llm_fallback_prompt_is_capped_and_keeps_the_refusal_option(services):
    seen = {}

    class R:
        async def generate(self, cap, messages, **kw):
            seen["prompt"], seen["enum"] = messages[-1]["content"], kw["schema"]["properties"]["choice"]["enum"]
            return type("G", (), {"json": lambda self: {"choice": "none", "reason": "x"}, "model": "m", "duration_ms": 1})()
    services.router = R()
    opts = {f"e{i}": f"link 'Item {i}'" for i in range(300)} | {"none": "none of these elements matches"}
    out = await Decisions(services).choose("Page", "Which element is the search box?", opts, goal="search box", use_laya=False)
    assert out["choice"] == "none" and len(seen["enum"]) <= LLM_MAX_OPTIONS + 1 and "none" in seen["enum"]
    assert seen["prompt"].count("\n- ") <= LLM_MAX_OPTIONS + 1


def test_read_only_get_form_buttons_are_not_risky_but_post_forms_stay_gated():
    assert is_risky_control("Seek", "submit", readonly_form=True) is False         # search form's go button
    assert is_risky_control("Seek", "submit", readonly_form=False) is True         # unknown POST form: still gated
    assert is_risky_control("Submit application", "submit", readonly_form=True) is True
    assert is_risky_control("Send", "submit", readonly_form=True) is True


async def test_router_warm_loads_main_and_embedding_models_with_the_real_context_size(services):
    import httpx

    from forge.config import ModelConfig, NodeConfig
    from forge.models.router import ModelRouter
    services.cfg.nodes = [NodeConfig(name="local", url="http://ollama.test", kind="ollama")]
    services.cfg.models = [ModelConfig(id="main", node="local", capabilities=["tool_calling"], ctx=16384),
                           ModelConfig(id="emb", node="local", capabilities=["embedding"], ctx=8192)]
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        import json
        calls.append((req.url.path, json.loads(req.content)))
        return httpx.Response(200, json={"embeddings": [[0.0]]} if req.url.path == "/api/embed" else {"done": True})
    r = ModelRouter(services.cfg)
    r.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    r.nodes["local"].healthy = True
    timings = await r.warm()
    assert set(timings) == {"main", "emb"}
    by_path = dict(calls)
    assert by_path["/api/generate"]["options"]["num_ctx"] == 16384 and by_path["/api/generate"]["keep_alive"] == "30m"
    assert by_path["/api/embed"]["options"]["num_ctx"] == 8192


def test_single_model_routes_all_text_work_to_one_model_but_not_embeddings():
    from forge.config import ForgeConfig, ModelConfig, NodeConfig
    from forge.models.router import ModelRouter
    cfg = ForgeConfig(nodes=[NodeConfig(name="n", url="http://x", kind="ollama")],
                      models=[ModelConfig(id="big", node="n", capabilities=["reasoning", "coding", "tool_calling"]),
                              ModelConfig(id="small", node="n", capabilities=["fast", "extraction"]),
                              ModelConfig(id="emb", node="n", capabilities=["embedding"])])
    cfg.routing.single_model = "big"
    r = ModelRouter(cfg)
    assert [m.id for m in r.candidates("extraction")][0] == "big"
    assert [m.id for m in r.candidates("fast")][0] == "big"
    assert [m.id for m in r.candidates("embedding")] == ["emb"]
