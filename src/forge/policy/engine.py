"""Default-deny policy engine.

Order of checks for every tool call (agent or workflow):
  1. emergency stop flag        -> EmergencyStop
  2. tool not granted           -> PermissionDenied
  3. capability not granted     -> PermissionDenied (desktop, browser, shell)
  4. path / url outside grants  -> PermissionDenied (workflow) or permission approval (interactive session)
  5. protected system location  -> PermissionDenied, always, regardless of grants
  6. side_effect == external    -> requires a signed, unexpired, single-use approval for this exact action
"""
from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from pydantic import BaseModel, Field

from forge.config import forge_home
from forge.errors import EmergencyStop, NeedsApproval, PermissionDenied
from forge.tools.registry import SideEffect, ToolSpec


class Permissions(BaseModel):
    """Capability grant. Declared per workflow in IR, approved at deploy, frozen against repair."""
    tools: list[str] = Field(default_factory=list, description="tool name globs, e.g. records.* ")
    fs_read: list[str] = Field(default_factory=list)
    fs_write: list[str] = Field(default_factory=list)
    net: list[str] = Field(default_factory=list, description="host globs, e.g. *.seek.com.au")
    desktop: bool = False
    browser: bool = False
    shell: bool = False

    def allows_tool(self, name: str) -> bool:
        return any(fnmatch.fnmatchcase(name, p) for p in self.tools)

    def expanded(self) -> "Permissions":
        exp = lambda xs: [str(_norm(x)) for x in xs]
        return self.model_copy(update={"fs_read": exp(self.fs_read), "fs_write": exp(self.fs_write)})


CAP_FLAGS = {"desktop": "desktop", "browser": "browser", "shell": "shell"}


def _norm(p: str | Path) -> Path:
    s = os.path.expandvars(str(p)).replace("${FORGE_HOME}", str(forge_home()))
    return Path(s).expanduser().resolve(strict=False)


def protected_paths() -> list[Path]:
    home = forge_home()
    sysroot = os.environ.get("SystemRoot", r"C:\Windows")
    return [_norm(x) for x in (
        sysroot, r"C:\Program Files", r"C:\Program Files (x86)", r"C:\ProgramData",
        home / "forge.db", home / "forge.db-wal", home / "forge.toml", home / "token", home / ".approval_key",
        Path.home() / ".ssh", Path.home() / "AppData" / "Roaming" / "Microsoft" / "Credentials")]


def _under(path: Path, roots: list[str]) -> bool:
    p = str(path).lower()
    for r in roots:
        rr = str(_norm(r)).lower().rstrip("\\/")
        if p == rr or p.startswith(rr + os.sep):
            return True
    return False


def host_allowed(url: str, allow: list[str]) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return bool(host) and any(fnmatch.fnmatchcase(host, pat.lower()) or host == pat.lower().lstrip("*.") for pat in allow)


@dataclass
class Decision:
    allowed: bool
    reason: str = ""
    approval_subject: dict | None = None


class PolicyEngine:
    def __init__(self, db, approvals):
        self.db = db
        self.approvals = approvals

    # -- kill switch ---------------------------------------------------------------
    def stopped(self) -> bool:
        return self.db.get_kv("emergency_stop", "0") == "1"

    def set_stop(self, on: bool, actor: str = "user") -> None:
        self.db.set_kv("emergency_stop", "1" if on else "0")
        self.db.audit(actor, "emergency_stop" if on else "emergency_resume", {})

    # -- main check ----------------------------------------------------------------
    def check(self, spec: ToolSpec, args: BaseModel, perms: Permissions, *, scope: dict, interactive: bool,
              dry_run: bool = False) -> Decision:
        """scope identifies who acts: {"workflow_version_id", "step"} or {"session_id"}. Raises on deny/approval.
        dry_run enforces every grant but does not request approvals (the side effect is mocked anyway)."""
        if self.stopped():
            raise EmergencyStop("emergency stop is active (forge resume to clear)")
        if not perms.allows_tool(spec.name):
            raise PermissionDenied(f"tool '{spec.name}' is not granted to this {'session' if interactive else 'workflow'}")
        for cap, flag in CAP_FLAGS.items():
            if cap in spec.caps and not getattr(perms, flag):
                raise PermissionDenied(f"tool '{spec.name}' needs the '{cap}' capability, which is not granted")

        data = args.model_dump(mode="json")
        writes = spec.side_effect != SideEffect.NONE
        for f in spec.path_fields:
            val = data.get(f)
            if not val:
                continue
            path = _norm(val)
            if writes and _under(path, [str(p) for p in protected_paths()]):
                raise PermissionDenied(f"'{path}' is a protected location")
            roots = perms.fs_write if writes else perms.fs_read + perms.fs_write
            if not _under(path, roots):
                self._scope_violation(interactive, scope, "fs_write" if writes else "fs_read", str(path), spec.name)
        for f in spec.url_fields:
            val = data.get(f)
            if val and not host_allowed(val, perms.net):
                self._scope_violation(interactive, scope, "net", urlparse(val).hostname or val, spec.name)

        if spec.side_effect == SideEffect.EXTERNAL and dry_run:
            return Decision(True, "dry_run")
        if spec.side_effect == SideEffect.EXTERNAL:
            subject = {"type": "action", "tool": spec.name, "args": data, **scope}
            status, row = self.approvals.status_for(subject)
            if status == "approved":
                self.approvals.consume(row["id"])
                return Decision(True, "approved", subject)
            if status == "denied":
                raise PermissionDenied(f"user denied {spec.name}: {row.get('reason') or ''}".strip())
            row = self.approvals.request("action", f"{spec.name}: {_summ(data)}", subject,
                                         requested_by=scope.get("run_id") or scope.get("session_id") or "agent")
            raise NeedsApproval(f"{spec.name} is an external action and needs approval", row["id"])
        return Decision(True, "granted")

    def _scope_violation(self, interactive: bool, scope: dict, kind: str, value: str, tool: str) -> None:
        if not interactive:
            # Workflows must declare everything at compile time; widening is a new deploy approval.
            raise PermissionDenied(f"{tool}: {kind} '{value}' is outside this workflow's approved permissions")
        subject = {"type": "permission", "kind": kind, "value": value, "session_id": scope.get("session_id")}
        status, row = self.approvals.status_for(subject)
        if status == "approved":
            return  # session-scoped widening, approved by the user
        if status == "denied":
            raise PermissionDenied(f"user denied {kind} access to '{value}'")
        row = self.approvals.request("permission", f"allow {kind} → {value} (for {tool})", subject,
                                     requested_by=scope.get("session_id") or "agent")
        raise NeedsApproval(f"{kind} access to '{value}' needs approval", row["id"])


def _summ(d: dict, n: int = 90) -> str:
    s = ", ".join(f"{k}={v!r}" for k, v in d.items())
    return s if len(s) <= n else s[: n - 1] + "…"


def session_permissions(workspace: Path) -> Permissions:
    """Chat sessions: all tools visible, reads in user profile, writes in workspace, no net until approved."""
    return Permissions(tools=["*"], fs_read=[str(Path.home())], fs_write=[str(workspace)], net=["127.0.0.1", "localhost"],
                       desktop=True, browser=True, shell=False)
