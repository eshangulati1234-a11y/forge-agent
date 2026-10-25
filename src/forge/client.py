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


def _ok(r: httpx.Response) -> Any:
    if r.status_code >= 400:
        try:
            d = r.json()
            msg = d.get("error") or d.get("detail") or r.text
        except Exception:
            msg = r.text
        raise ClientError(f"{r.status_code}: {msg}")
    return r.json()
