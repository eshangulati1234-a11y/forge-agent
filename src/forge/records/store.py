"""Generic structured records. Plugins declare collections (fields, enums, key fields, status transitions);
the store enforces validation, stable IDs, optimistic versions and an auditable field-level event history.
"""
from __future__ import annotations

import datetime as dt
import json
import re
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field

from forge.db import DB, canonical, now, sha
from forge.errors import InvalidInput


class FieldSpec(BaseModel):
    name: str
    type: Literal["string", "text", "integer", "number", "boolean", "date", "datetime", "url", "enum", "email"] = "string"
    enum: list[str] | None = None
    required: bool = False
    width: int = 18
    description: str = ""
    min: float | None = None
    max: float | None = None


class CollectionSpec(BaseModel):
    name: str
    title: str
    id_prefix: str = "REC"
    key_fields: list[str] = Field(default_factory=list, description="fields hashed into a stable ID")
    fields: list[FieldSpec]
    status_field: str | None = None
    transitions: dict[str, list[str]] = Field(default_factory=dict)   # from -> allowed to
    initial_status: str | None = None

    def field(self, name: str) -> FieldSpec | None:
        return next((f for f in self.fields if f.name == name), None)


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def coerce(spec: FieldSpec, value: Any) -> Any:
    if value is None or value == "":
        return None
    t = spec.type
    try:
        if t == "integer":
            v = int(float(value))
        elif t == "number":
            v = float(value)
        elif t == "boolean":
            v = value if isinstance(value, bool) else str(value).strip().lower() in ("1", "true", "yes", "y")
        elif t == "date":
            if isinstance(value, dt.datetime):
                v = value.date().isoformat()
            elif isinstance(value, dt.date):
                v = value.isoformat()
            else:
                v = dt.date.fromisoformat(str(value)[:10]).isoformat()
        elif t == "datetime":
            v = value.isoformat() if isinstance(value, dt.datetime) else dt.datetime.fromisoformat(str(value)).isoformat()
        elif t == "enum":
            v = str(value)
            if spec.enum and v not in spec.enum:
                raise ValueError(f"must be one of {spec.enum}")
        elif t == "url":
            v = str(value).strip()
            if not v.startswith(("http://", "https://")):
                raise ValueError("must be an http(s) URL")
        elif t == "email":
            v = str(value).strip()
            if not EMAIL_RE.match(v):
                raise ValueError("must be an email address")
        else:
            v = str(value)
    except (TypeError, ValueError) as e:
        raise InvalidInput(f"field '{spec.name}': {e} (got {value!r})") from None
    if spec.min is not None and isinstance(v, (int, float)) and v < spec.min:
        raise InvalidInput(f"field '{spec.name}' must be >= {spec.min}")
    if spec.max is not None and isinstance(v, (int, float)) and v > spec.max:
        raise InvalidInput(f"field '{spec.name}' must be <= {spec.max}")
    return v


class RecordStore:
    def __init__(self, db: DB, events=None):
        self.db = db
        self.events = events
        self.collections: dict[str, CollectionSpec] = {}

    def register(self, spec: CollectionSpec) -> None:
        self.collections[spec.name] = spec

    def load_yaml(self, text: str) -> list[str]:
        data = yaml.safe_load(text)
        names = []
        for c in data.get("collections", []):
            spec = CollectionSpec.model_validate(c)
            self.register(spec)
            names.append(spec.name)
        return names

    def spec(self, collection: str) -> CollectionSpec:
        if collection not in self.collections:
            raise InvalidInput(f"unknown collection '{collection}'; known: {sorted(self.collections)}")
        return self.collections[collection]

    def stable_id(self, spec: CollectionSpec, data: dict) -> str:
        if spec.key_fields and all(data.get(k) for k in spec.key_fields):
            basis = "|".join(str(data[k]).strip().lower() for k in spec.key_fields)
        else:
            basis = canonical(data) + str(now())
        return f"{spec.id_prefix}-{sha(basis)[:10].upper()}"

    def validate(self, spec: CollectionSpec, data: dict, partial: bool = False) -> dict:
        unknown = set(data) - {f.name for f in spec.fields} - {"id"}
        if unknown:
            raise InvalidInput(f"unknown fields for {spec.name}: {sorted(unknown)}")
        out = {}
        for f in spec.fields:
            if f.name in data:
                out[f.name] = coerce(f, data[f.name])
            if not partial and f.required and out.get(f.name) is None:
                raise InvalidInput(f"field '{f.name}' is required for {spec.name}")
        return out

    def get(self, collection: str, record_id: str) -> dict | None:
        row = self.db.one("SELECT * FROM records WHERE collection=? AND id=? AND deleted=0", (collection, record_id))
        if not row:
            return None
        return {"id": row["id"], **json.loads(row["data_json"]), "_version": row["version"], "_updated_at": row["updated_at"]}

    def list(self, collection: str, where: dict | None = None, limit: int = 1000) -> list[dict]:
        rows = self.db.all("SELECT * FROM records WHERE collection=? AND deleted=0 ORDER BY created_at LIMIT ?", (collection, limit))
        out = [{"id": r["id"], **json.loads(r["data_json"]), "_version": r["version"]} for r in rows]
        if where:
            out = [r for r in out if all(r.get(k) == v for k, v in where.items())]
        return out

    def upsert(self, collection: str, data: dict, actor: str, *, expected_version: int | None = None) -> dict:
        spec = self.spec(collection)
        rid = data.get("id")
        existing = None
        with self.db.tx() as db:
            if not rid:
                rid = self.stable_id(spec, {k: data.get(k) for k in spec.key_fields} if spec.key_fields else data)
            row = db.one("SELECT * FROM records WHERE collection=? AND id=?", (collection, rid))
            if row and not row["deleted"]:
                existing = json.loads(row["data_json"])
                if expected_version is not None and row["version"] != expected_version:
                    raise InvalidInput(f"{rid}: version conflict (have {row['version']}, expected {expected_version})")
                clean = self.validate(spec, {k: v for k, v in data.items() if k != "id"}, partial=True)
                self._check_transition(spec, existing, clean)
                merged = {**existing, **clean}
                changes = {k: (existing.get(k), v) for k, v in merged.items() if existing.get(k) != v}
                if not changes:
                    return {"id": rid, "created": False, "changed": []}
                db.execute("UPDATE records SET data_json=?, version=version+1, updated_at=? WHERE collection=? AND id=?",
                           (canonical(merged), now(), collection, rid))
            else:
                payload = {k: v for k, v in data.items() if k != "id"}
                if spec.status_field and spec.initial_status and not payload.get(spec.status_field):
                    payload[spec.status_field] = spec.initial_status
                clean = self.validate(spec, payload)
                merged = clean
                changes = {k: (None, v) for k, v in clean.items() if v is not None}
                db.execute("INSERT INTO records(collection,id,data_json,version,created_at,updated_at,deleted) VALUES(?,?,?,1,?,?,0)"
                           " ON CONFLICT(collection,id) DO UPDATE SET data_json=excluded.data_json, deleted=0, version=records.version+1, updated_at=excluded.updated_at",
                           (collection, rid, canonical(clean), now(), now()))
            for k, (old, new) in changes.items():
                db.execute("INSERT INTO record_events(collection,record_id,ts,actor,field,old_value,new_value) VALUES(?,?,?,?,?,?,?)",
                           (collection, rid, now(), actor, k, None if old is None else str(old), None if new is None else str(new)))
        if self.events:
            self.events.emit("record.changed", collection=collection, record_id=rid, fields=sorted(changes))
        return {"id": rid, "created": existing is None, "changed": sorted(changes)}

    def _check_transition(self, spec: CollectionSpec, existing: dict, clean: dict) -> None:
        sf = spec.status_field
        if not sf or sf not in clean or not spec.transitions:
            return
        old, new = existing.get(sf), clean[sf]
        if old == new or old is None:
            return
        if new not in spec.transitions.get(old, []):
            raise InvalidInput(f"illegal status transition {old!r} -> {new!r}; allowed from {old!r}: {spec.transitions.get(old, [])}")

    def history(self, collection: str, record_id: str | None = None, limit: int = 500) -> list[dict]:
        if record_id:
            return self.db.all("SELECT * FROM record_events WHERE collection=? AND record_id=? ORDER BY id DESC LIMIT ?",
                               (collection, record_id, limit))
        return self.db.all("SELECT * FROM record_events WHERE collection=? ORDER BY id DESC LIMIT ?", (collection, limit))

    def delete(self, collection: str, record_id: str, actor: str) -> None:
        self.db.execute("UPDATE records SET deleted=1, version=version+1, updated_at=? WHERE collection=? AND id=?", (now(), collection, record_id))
        self.db.execute("INSERT INTO record_events(collection,record_id,ts,actor,field,old_value,new_value) VALUES(?,?,?,?,?,?,?)",
                        (collection, record_id, now(), actor, "_deleted", "0", "1"))
