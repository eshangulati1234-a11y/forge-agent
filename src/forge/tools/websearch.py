"""web.search through the user's own SearXNG instance: no API key, no third-party account. Results are untrusted data."""
from __future__ import annotations

import httpx
from pydantic import BaseModel, Field

from forge.errors import InvalidInput, ResourceUnavailable
from forge.tools.registry import ToolContext, tool


class WebSearch(BaseModel):
    query: str = Field(min_length=1)
    n: int = Field(5, ge=1, le=10)


@tool("web.search", WebSearch, description="Search the web (SearXNG) for titles, URLs and snippets; use it to look things up instead of guessing",
      caps={"net"})
async def web_search(a: WebSearch, ctx: ToolContext):
    base = (ctx.services.cfg.search.searxng_url or "").rstrip("/")
    if not base:
        raise InvalidInput("web.search is not configured: run SearXNG (docker run -p 8888:8080 searxng/searxng) and "
                           "set search.searxng_url to http://127.0.0.1:8888")
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get(f"{base}/search", params={"q": a.query, "format": "json"})
            r.raise_for_status()
    except httpx.HTTPError as e:
        raise ResourceUnavailable(f"SearXNG at {base} failed: {e} (is format=json enabled in its settings?)") from e
    items = r.json().get("results", [])[: a.n]
    return {"results": [{"title": i.get("title", ""), "url": i.get("url", ""), "snippet": (i.get("content") or "")[:300]} for i in items]}
