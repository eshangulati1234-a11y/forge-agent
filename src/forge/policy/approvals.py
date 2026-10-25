"""Approval records: HMAC-signed, bound to an action hash, expiring.

The signing key lives in Windows Credential Manager (keyring). Anything that edits the DB without the key
(including a compromised tool writing to forge.db) cannot forge an `approved` row that verifies.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
from typing import Any

from forge.db import DB, canonical, new_id, now, sha

KEYRING_SERVICE = "forge"
KEYRING_USER = "approval-signing-key"
DEFAULT_TTL = 24 * 3600


def _signing_key() -> bytes:
    try:
        import keyring
        k = keyring.get_password(KEYRING_SERVICE, KEYRING_USER)
        if not k:
            k = secrets.token_hex(32)
            keyring.set_password(KEYRING_SERVICE, KEYRING_USER, k)
        return bytes.fromhex(k)
    except Exception:  # headless test env without a keyring backend
        from forge.config import forge_home
        p = forge_home() / ".approval_key"
        if not p.exists():
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(secrets.token_hex(32))
        return bytes.fromhex(p.read_text().strip())


def action_hash(subject: dict) -> str:
    return sha(subject)


class Approvals:
    def __init__(self, db: DB, events=None, key: bytes | None = None):
        self.db = db
        self.events = events
        self._key = key

    @property
    def key(self) -> bytes:
        if self._key is None:
            self._key = _signing_key()
        return self._key

    def _sign(self, row: dict) -> str:
        msg = canonical({k: row[k] for k in ("id", "kind", "action_hash", "status", "decided_at", "expires_at", "decided_by")})
        return hmac.new(self.key, msg.encode(), hashlib.sha256).hexdigest()

    def request(self, kind: str, title: str, subject: dict, requested_by: str, ttl: float = DEFAULT_TTL) -> dict:
        h = action_hash(subject)
        existing = self.db.one("SELECT * FROM approvals WHERE action_hash=? AND status='pending'", (h,))
        if existing:
            return existing
        row = {"id": new_id("ap_"), "kind": kind, "title": title, "subject_json": canonical(subject), "action_hash": h,
               "status": "pending", "requested_by": requested_by, "requested_at": now(), "expires_at": now() + ttl}
        self.db.execute("INSERT INTO approvals(id,kind,title,subject_json,action_hash,status,requested_by,requested_at,expires_at)"
                        " VALUES(:id,:kind,:title,:subject_json,:action_hash,:status,:requested_by,:requested_at,:expires_at)", row)
        self.db.audit(requested_by, "approval.requested", {"id": row["id"], "kind": kind, "title": title})
        if self.events:
            self.events.emit("approval.requested", approval_id=row["id"], title=title, approval_kind=kind)
        return row

    def decide(self, approval_id: str, approve: bool, decided_by: str = "user", reason: str = "") -> dict:
        row = self.db.one("SELECT * FROM approvals WHERE id=?", (approval_id,))
        if not row:
            raise KeyError(approval_id)
        if row["status"] != "pending":
            raise ValueError(f"approval {approval_id} already {row['status']}")
        if row["expires_at"] < now():
            self.db.execute("UPDATE approvals SET status='expired' WHERE id=?", (approval_id,))
            raise ValueError(f"approval {approval_id} expired")
        row.update(status="approved" if approve else "denied", decided_at=now(), decided_by=decided_by, reason=reason)
        row["signature"] = self._sign(row)
        self.db.execute("UPDATE approvals SET status=?,decided_at=?,decided_by=?,reason=?,signature=? WHERE id=?",
                        (row["status"], row["decided_at"], decided_by, reason, row["signature"], approval_id))
        self.db.audit(decided_by, f"approval.{row['status']}", {"id": approval_id, "reason": reason})
        if self.events:
            self.events.emit("approval.decided", approval_id=approval_id, status=row["status"])
        return row

    def verify(self, row: dict) -> bool:
        return bool(row.get("signature")) and hmac.compare_digest(row["signature"], self._sign(row))

    def status_for(self, subject: dict) -> tuple[str, dict | None]:
        """Return ('approved'|'denied'|'pending'|'none', row) for this exact action. Signature is enforced."""
        h = action_hash(subject)
        row = self.db.one("SELECT * FROM approvals WHERE action_hash=? ORDER BY requested_at DESC LIMIT 1", (h,))
        if not row:
            return "none", None
        if row["status"] in ("approved", "denied"):
            if not self.verify(row):
                self.db.audit("system", "approval.signature_invalid", {"id": row["id"]})
                return "none", None
            if row["status"] == "approved" and (row["expires_at"] < now() or row.get("used_at")):
                return "none", None
        if row["status"] == "pending" and row["expires_at"] < now():
            self.db.execute("UPDATE approvals SET status='expired' WHERE id=?", (row["id"],))
            return "none", None
        return row["status"], row

    def consume(self, approval_id: str) -> None:
        self.db.execute("UPDATE approvals SET used_at=? WHERE id=? AND used_at IS NULL", (now(), approval_id))

    def pending(self) -> list[dict]:
        return self.db.all("SELECT * FROM approvals WHERE status='pending' AND expires_at>? ORDER BY requested_at", (now(),))

    def get(self, approval_id: str) -> dict[str, Any] | None:
        return self.db.one("SELECT * FROM approvals WHERE id=?", (approval_id,))
