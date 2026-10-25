"""Capability-based model router over independent inference nodes (Ollama native or OpenAI-compatible).

GPUs are never pooled: each node serves whole models. The router picks, per request, the best healthy
(model, node) for a capability considering quality, context fit, whether the model is already resident
(avoids a multi-second swap on an 8 GB card), per-node queue depth, and a circuit breaker on failures.
NVIDIA PAIR or llama.cpp servers slot in later as `kind = "openai"` nodes.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

import httpx

from forge.config import ForgeConfig, ModelConfig, NodeConfig
from forge.errors import ErrorClass, ForgeError, TransientError

log = logging.getLogger("forge.router")

SWAP_PENALTY = 1.5      # quality points; a cold model on the 8 GB card costs ~5-10 s to load
QUEUE_PENALTY = 0.75
FAIL_THRESHOLD = 3
COOLDOWN_S = 60.0


@dataclass
class NodeState:
    cfg: NodeConfig
    sem: asyncio.Semaphore
    healthy: bool = True
    failures: int = 0
    open_until: float = 0.0
    queued: int = 0
    loaded: set[str] = field(default_factory=set)
    last_check: float = 0.0
    latency_ms: float = 0.0
    vram_used_mb: int | None = None

    def available(self) -> bool:
        return self.healthy and time.time() >= self.open_until


@dataclass
class GenResult:
    content: str
    tool_calls: list[dict]
    model: str
    node: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    duration_ms: int = 0
    raw: dict | None = None

    def json(self) -> Any:
        txt = self.content.strip()
        if txt.startswith("```"):
            txt = txt.strip("`").split("\n", 1)[-1].rsplit("```", 1)[0]
        return json.loads(txt)


def estimate_tokens(messages: list[dict]) -> int:
    return sum(len(str(m.get("content", ""))) for m in messages) // 3 + 50 * len(messages)


class ModelRouter:
    def __init__(self, cfg: ForgeConfig, events=None):
        self.cfg = cfg
        self.events = events
        self.nodes = {n.name: NodeState(n, asyncio.Semaphore(n.max_parallel)) for n in cfg.nodes}
        self.models = cfg.models
        self.pinned: dict[str, str] = {}   # capability -> model id override (TUI /model)
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(300, connect=5))

    # ---- health -----------------------------------------------------------------
    async def refresh(self) -> None:
        await asyncio.gather(*(self._probe(n) for n in self.nodes.values()))

    async def _probe(self, n: NodeState) -> None:
        t = time.time()
        try:
            if n.cfg.kind == "ollama":
                r = await self.client.get(f"{n.cfg.url}/api/ps", timeout=3)
                r.raise_for_status()
                n.loaded = {m["name"] for m in r.json().get("models", [])}
            else:
                r = await self.client.get(f"{n.cfg.url}/v1/models", headers=self._headers(n), timeout=3)
                r.raise_for_status()
            n.healthy, n.latency_ms = True, (time.time() - t) * 1000
            if n.failures >= FAIL_THRESHOLD and time.time() >= n.open_until:
                n.failures = 0
        except Exception as e:
            n.healthy = False
            log.warning("node %s unhealthy: %s", n.cfg.name, e)
        n.last_check = time.time()

    def status(self) -> list[dict]:
        return [{"name": n.cfg.name, "url": n.cfg.url, "kind": n.cfg.kind, "healthy": n.available(), "queued": n.queued,
                 "loaded": sorted(n.loaded), "latency_ms": round(n.latency_ms, 1), "failures": n.failures}
                for n in self.nodes.values()]

    # ---- selection ----------------------------------------------------------------
    def candidates(self, capability: str, need_ctx: int = 0, complexity: str = "normal") -> list[ModelConfig]:
        ms = [m for m in self.models if capability in m.capabilities and m.node in self.nodes]
        if capability in self.pinned:
            ms.sort(key=lambda m: m.id != self.pinned[capability])
            return ms

        def score(m: ModelConfig) -> float:
            n = self.nodes[m.node]
            s = float(m.quality)
            if complexity == "low":
                s = 5 - s   # prefer small/fast models for trivial work
            if not n.available():
                s -= 100
            if m.ctx < need_ctx:
                s -= 50
            if n.cfg.kind == "ollama" and m.id not in n.loaded:
                s -= SWAP_PENALTY
            s -= QUEUE_PENALTY * n.queued
            return s
        return sorted(ms, key=score, reverse=True)

    # ---- generation -------------------------------------------------------------------
    async def generate(self, capability: str, messages: list[dict], *, tools: list[dict] | None = None,
                       schema: dict | str | None = None, temperature: float = 0.2, max_tokens: int = 2048,
                       complexity: str = "normal", images: list[str] | None = None, think: bool | None = None) -> GenResult:
        need = estimate_tokens(messages) + max_tokens
        cands = self.candidates(capability, need, complexity)
        if not cands:
            raise ForgeError(f"no model provides capability '{capability}'", ErrorClass.RESOURCE_UNAVAILABLE)
        last: Exception | None = None
        for m in cands:
            n = self.nodes[m.node]
            if not n.available():
                continue
            try:
                return await self._call(n, m, messages, tools, schema, temperature, max_tokens, images, think)
            except (httpx.TransportError, httpx.HTTPStatusError, TransientError) as e:
                last = e
                self._fail(n, e)
                continue
        raise TransientError(f"all nodes failed for '{capability}': {last}")

    async def stream(self, capability: str, messages: list[dict], *, tools: list[dict] | None = None,
                     temperature: float = 0.3, max_tokens: int = 2048) -> AsyncIterator[dict]:
        """Yields {"delta": str} chunks then a final {"done": GenResult}. Ollama nodes only stream natively."""
        need = estimate_tokens(messages) + max_tokens
        for m in self.candidates(capability, need):
            n = self.nodes[m.node]
            if not n.available():
                continue
            if n.cfg.kind != "ollama":
                res = await self._call(n, m, messages, tools, None, temperature, max_tokens, None, None)
                yield {"delta": res.content}
                yield {"done": res}
                return
            try:
                async for chunk in self._ollama_stream(n, m, messages, tools, temperature, max_tokens):
                    yield chunk
                return
            except (httpx.TransportError, httpx.HTTPStatusError) as e:
                self._fail(n, e)
        raise TransientError(f"no healthy node for '{capability}'")

    def _fail(self, n: NodeState, e: Exception) -> None:
        n.failures += 1
        log.warning("node %s failure %d: %s", n.cfg.name, n.failures, e)
        if n.failures >= FAIL_THRESHOLD:
            n.open_until = time.time() + COOLDOWN_S
            if self.events:
                self.events.emit("node.circuit_open", node=n.cfg.name, error=str(e)[:200])

    def _headers(self, n: NodeState) -> dict:
        return {"Authorization": f"Bearer {n.cfg.api_key}"} if n.cfg.api_key else {}

    def _ollama_body(self, m: ModelConfig, messages, tools, temperature, max_tokens, think, stream: bool) -> dict:
        body: dict[str, Any] = {"model": m.id, "messages": messages, "stream": stream,
                                "options": {"temperature": temperature, "num_ctx": m.ctx, "num_predict": max_tokens},
                                "keep_alive": "30m"}
        if "qwen3" in m.id:
            body["think"] = m.think if think is None else think
        if tools:
            body["tools"] = tools
        return body

    async def _call(self, n: NodeState, m: ModelConfig, messages, tools, schema, temperature, max_tokens, images, think) -> GenResult:
        n.queued += 1
        t = time.time()
        try:
            async with n.sem:
                if n.cfg.kind == "ollama":
                    msgs = [dict(x) for x in messages]
                    if images:
                        msgs[-1]["images"] = images
                    body = self._ollama_body(m, msgs, tools, temperature, max_tokens, think, stream=False)
                    if schema:
                        body["format"] = schema
                    r = await self.client.post(f"{n.cfg.url}/api/chat", json=body, headers=self._headers(n))
                    r.raise_for_status()
                    d = r.json()
                    msg = d.get("message", {})
                    n.loaded.add(m.id)
                    calls = [{"name": c["function"]["name"], "arguments": c["function"].get("arguments") or {}}
                             for c in msg.get("tool_calls") or []]
                    res = GenResult(msg.get("content", ""), calls, m.id, n.cfg.name, d.get("prompt_eval_count", 0),
                                    d.get("eval_count", 0), int((time.time() - t) * 1000), d)
                else:
                    body = {"model": m.id, "messages": messages, "temperature": temperature, "max_tokens": max_tokens}
                    if tools:
                        body["tools"] = tools
                    if schema == "json":
                        body["response_format"] = {"type": "json_object"}
                    elif schema:
                        body["response_format"] = {"type": "json_schema", "json_schema": {"name": "out", "schema": schema}}
                    r = await self.client.post(f"{n.cfg.url}/v1/chat/completions", json=body, headers=self._headers(n))
                    r.raise_for_status()
                    d = r.json()
                    msg = d["choices"][0]["message"]
                    calls = [{"name": c["function"]["name"], "arguments": _maybe_json(c["function"].get("arguments"))}
                             for c in msg.get("tool_calls") or []]
                    u = d.get("usage", {})
                    res = GenResult(msg.get("content") or "", calls, m.id, n.cfg.name, u.get("prompt_tokens", 0),
                                    u.get("completion_tokens", 0), int((time.time() - t) * 1000), d)
            n.failures = 0
            if self.events:
                self.events.emit("model.call", model=m.id, node=n.cfg.name, ms=res.duration_ms,
                                 tokens_in=res.prompt_tokens, tokens_out=res.completion_tokens)
            return res
        finally:
            n.queued -= 1

    async def _ollama_stream(self, n, m, messages, tools, temperature, max_tokens) -> AsyncIterator[dict]:
        n.queued += 1
        t = time.time()
        content, calls, final = [], [], {}
        try:
            async with n.sem:
                body = self._ollama_body(m, messages, tools, temperature, max_tokens, None, stream=True)
                async with self.client.stream("POST", f"{n.cfg.url}/api/chat", json=body, headers=self._headers(n)) as r:
                    r.raise_for_status()
                    async for line in r.aiter_lines():
                        if not line:
                            continue
                        d = json.loads(line)
                        msg = d.get("message", {})
                        if msg.get("content"):
                            content.append(msg["content"])
                            yield {"delta": msg["content"]}
                        for c in msg.get("tool_calls") or []:
                            calls.append({"name": c["function"]["name"], "arguments": c["function"].get("arguments") or {}})
                        if d.get("done"):
                            final = d
            n.loaded.add(m.id)
            n.failures = 0
            yield {"done": GenResult("".join(content), calls, m.id, n.cfg.name, final.get("prompt_eval_count", 0),
                                     final.get("eval_count", 0), int((time.time() - t) * 1000), final)}
        finally:
            n.queued -= 1

    async def embed(self, texts: list[str]) -> list[list[float]]:
        for m in self.candidates("embedding"):
            n = self.nodes[m.node]
            if not n.available():
                continue
            try:
                async with n.sem:
                    if n.cfg.kind == "ollama":
                        r = await self.client.post(f"{n.cfg.url}/api/embed", json={"model": m.id, "input": texts, "keep_alive": "30m"})
                        r.raise_for_status()
                        return r.json()["embeddings"]
                    r = await self.client.post(f"{n.cfg.url}/v1/embeddings", json={"model": m.id, "input": texts},
                                               headers=self._headers(n))
                    r.raise_for_status()
                    return [x["embedding"] for x in r.json()["data"]]
            except (httpx.TransportError, httpx.HTTPStatusError) as e:
                self._fail(n, e)
        raise TransientError("no embedding model available")

    async def aclose(self) -> None:
        await self.client.aclose()


def _maybe_json(v):
    if isinstance(v, str):
        try:
            return json.loads(v)
        except json.JSONDecodeError:
            return {"_raw": v}
    return v or {}
