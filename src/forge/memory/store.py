"""Typed long-term memory with provenance.

Provenance decides how a memory may be used:
  user / verified  -> injected as facts
  tool_observed    -> injected as observations with source
  generated        -> injected only as hypotheses, never as facts (an LLM saying it does not make it true)
Retrieval is brute-force cosine over local embeddings (qwen3-embedding); fine below ~100k memories.
"""
from __future__ import annotations

import json
import logging
from typing import Any

import numpy as np

from forge.db import DB, canonical, now

log = logging.getLogger("forge.memory")
KINDS = {"preference", "fact", "episode", "skill", "failure"}
PROVENANCE = {"user", "verified", "tool_observed", "generated"}
DEDUPE_COS = 0.95


class MemoryStore:
    def __init__(self, db: DB, router=None):
        self.db = db
        self.router = router

    async def _embed(self, text: str) -> bytes | None:
        if not self.router:
            return None
        try:
            v = np.asarray((await self.router.embed([text]))[0], dtype=np.float32)
            return (v / (np.linalg.norm(v) or 1)).tobytes()
        except Exception as e:  # memory must still work with the GPU node down
            log.warning("embedding unavailable: %s", e)
            return None

    async def add(self, kind: str, content: str, provenance: str, meta: dict | None = None) -> dict:
        if kind not in KINDS or provenance not in PROVENANCE:
            raise ValueError(f"kind must be in {KINDS}, provenance in {PROVENANCE}")
        emb = await self._embed(content)
        if emb is not None:
            for m in self._candidates(kinds=[kind]):
                if m["embedding"] and float(np.dot(np.frombuffer(m["embedding"], np.float32), np.frombuffer(emb, np.float32))) > DEDUPE_COS:
                    # Same memory restated. Upgrade provenance if the new source is stronger.
                    if _rank(provenance) > _rank(m["provenance"]):
                        self.db.execute("UPDATE memories SET provenance=?, content=? WHERE id=?", (provenance, content, m["id"]))
                    self.db.execute("UPDATE memories SET last_used_at=? WHERE id=?", (now(), m["id"]))
                    return {"id": m["id"], "deduplicated": True}
        cur = self.db.execute("INSERT INTO memories(kind,provenance,content,meta_json,embedding,created_at) VALUES(?,?,?,?,?,?)",
                              (kind, provenance, content, canonical(meta or {}), emb, now()))
        return {"id": cur.lastrowid, "deduplicated": False}

    def _candidates(self, kinds: list[str] | None = None, provenance: list[str] | None = None) -> list[dict]:
        sql, params = "SELECT * FROM memories WHERE invalidated_at IS NULL", []
        if kinds:
            sql += f" AND kind IN ({','.join('?' * len(kinds))})"
            params += kinds
        if provenance:
            sql += f" AND provenance IN ({','.join('?' * len(provenance))})"
            params += provenance
        return self.db.all(sql, params)

    async def search(self, query: str, k: int = 8, kinds: list[str] | None = None, provenance: list[str] | None = None) -> list[dict]:
        cands = self._candidates(kinds, provenance)
        q = await self._embed(query)
        if q is not None:
            qv = np.frombuffer(q, np.float32)
            scored = [(float(np.dot(qv, np.frombuffer(m["embedding"], np.float32))) if m["embedding"] else 0.0, m) for m in cands]
        else:
            words = set(query.lower().split())
            scored = [(sum(w in m["content"].lower() for w in words) / (len(words) or 1), m) for m in cands]
        scored.sort(key=lambda x: -x[0])
        out = []
        for s, m in scored[:k]:
            if s <= 0.2 and q is not None:
                continue
            self.db.execute("UPDATE memories SET last_used_at=? WHERE id=?", (now(), m["id"]))
            out.append({"id": m["id"], "kind": m["kind"], "provenance": m["provenance"], "content": m["content"],
                        "score": round(s, 3), "meta": json.loads(m["meta_json"])})
        return out

    async def context_block(self, query: str, k: int = 8) -> str:
        """Memory section for prompts, labelled so the model can tell facts from hypotheses."""
        mems = await self.search(query, k)
        if not mems:
            return ""
        lines = []
        for m in mems:
            tag = {"user": "FACT (user-stated)", "verified": "FACT (verified)", "tool_observed": "OBSERVED",
                   "generated": "UNVERIFIED HYPOTHESIS"}[m["provenance"]]
            lines.append(f"- [{tag}; {m['kind']}] {m['content']}")
        return "Relevant memory (only FACT entries may be relied upon as true):\n" + "\n".join(lines)

    def invalidate(self, memory_id: int, reason: str = "") -> None:
        self.db.execute("UPDATE memories SET invalidated_at=?, meta_json=json_set(meta_json,'$.invalidated_reason',?) WHERE id=?",
                        (now(), reason, memory_id))

    def verify(self, memory_id: int) -> None:
        self.db.execute("UPDATE memories SET provenance='verified' WHERE id=?", (memory_id,))

    def list(self, limit: int = 100) -> list[dict[str, Any]]:
        return self.db.all("SELECT id,kind,provenance,content,created_at,last_used_at FROM memories WHERE invalidated_at IS NULL"
                           " ORDER BY COALESCE(last_used_at, created_at) DESC LIMIT ?", (limit,))


def _rank(p: str) -> int:
    return {"generated": 0, "tool_observed": 1, "verified": 2, "user": 3}[p]
