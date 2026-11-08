"""Thin HTTP/WebSocket client for forged, shared by the CLI and the TUI."""
from __future__ import annotations

from typing import Any

import httpx

from forge.config import api_token, load_config


class ForgeClient:
    def __init__(self, base: str | None = None, token: str | None = None):
        cfg = load_config()
        self.base = base or f"http://{cfg.daemon.host}:{cfg.daemon.port}"
        self.token = token or api_token()
        self.http = httpx.AsyncClient(base_url=self.base, headers={"Authorization": f"Bearer {self.token}"}, timeout=600)

    @property
    def ws_url(self) -> str:
        return self.base.replace("http", "ws", 1) + f"/ws?token={self.token}"

    async def get(self, path: str, **params) -> Any:
        r = await self.http.get(path, params={k: v for k, v in params.items() if v is not None})
        return _ok(r)

    async def post(self, path: str, body: dict | None = None, **params) -> Any:
        r = await self.http.post(path, json=body or {}, params={k: v for k, v in params.items() if v is not None})
        return _ok(r)

    async def aclose(self) -> None:
        await self.http.aclose()


class SyncClient:
    def __init__(self):
        cfg = load_config()
        self.http = httpx.Client(base_url=f"http://{cfg.daemon.host}:{cfg.daemon.port}",
                                 headers={"Authorization": f"Bearer {api_token()}"}, timeout=900)

    def get(self, path: str, **params) -> Any:
        return _ok(self.http.get(path, params={k: v for k, v in params.items() if v is not None}))

    def post(self, path: str, body: dict | None = None, **params) -> Any:
        return _ok(self.http.post(path, json=body or {}, params={k: v for k, v in params.items() if v is not None}))


class ClientError(Exception):
    pass


async def read_chat(client, sid: str) -> tuple[dict, list[dict]]:
    """Read a saved chat, including from daemons started before history pagination existed."""
    legacy = False
    try:
        session = await client.get(f"/sessions/{sid}")
    except ClientError as e:
        if not str(e).startswith("404:"):
            raise
        # Older daemons expose the list and transcript but not the details route.
        rows = await client.get("/sessions")
        session = next((row for row in rows if row["id"] == sid), None)
        if session is None:
            # Distinguish a valid older chat outside the list limit from a missing ID.
            context = await client.get(f"/sessions/{sid}/context")
            session = {"id": sid, "title": "", "status": "running" if context.get("busy") else "idle"}
        legacy = True

    batches = []
    before_id = None
    limit = 200
    while True:
        batch = await client.get(f"/sessions/{sid}/messages", limit=limit, before_id=before_id)
        if legacy:
            # Those daemons ignore before_id: request a larger latest-message window instead.
            if len(batch) < limit:
                messages = batch
                break
            limit *= 2
            continue
        if not batch:
            break
        if before_id is not None and batch[-1]["id"] >= before_id:
            raise ClientError("The daemon does not support chat pagination. Restart Forge and try again.")
        batches.append(batch)
        if len(batch) < limit:
            break
        before_id = batch[0]["id"]
    if not legacy:
        messages = [message for batch in reversed(batches) for message in batch]
    if not session.get("title"):
        session = {**session, "title": next((m["content"][:60] for m in messages if m["role"] == "user"), "")}
    return session, messages


def _ok(r: httpx.Response) -> Any:
    if r.status_code >= 400:
        try:
            d = r.json()
            msg = d.get("error") or d.get("detail") or r.text
        except Exception:
            msg = r.text
        raise ClientError(f"{r.status_code}: {msg}")
    return r.json()
