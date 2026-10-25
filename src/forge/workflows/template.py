"""Sandboxed Jinja2 templating for workflow values.

A string that is exactly "{{ expr }}" evaluates to the native value (list, dict, number); anything else renders
to a string. The sandbox blocks attribute access to internals, so workflow YAML cannot execute arbitrary Python.
"""
from __future__ import annotations

import datetime as dt
import json
import re
from typing import Any

from jinja2 import StrictUndefined
from jinja2.sandbox import ImmutableSandboxedEnvironment

ENV = ImmutableSandboxedEnvironment(undefined=StrictUndefined, autoescape=False, keep_trailing_newline=True)
ENV.filters["tojson"] = lambda v, indent=None: json.dumps(v, default=str, indent=indent, ensure_ascii=False)
ENV.filters["truncate_chars"] = lambda v, n=2000: str(v)[:n]
ENV.globals["today"] = lambda: dt.date.today().isoformat()
ENV.globals["now_iso"] = lambda: dt.datetime.now().isoformat(timespec="seconds")
ENV.globals["days_from_today"] = lambda n: (dt.date.today() + dt.timedelta(days=int(n))).isoformat()

WHOLE = re.compile(r"^\s*\{\{(.*)\}\}\s*$", re.S)


def has_template(v: Any) -> bool:
    if isinstance(v, str):
        return "{{" in v or "{%" in v
    if isinstance(v, dict):
        return any(has_template(x) for x in v.values())
    if isinstance(v, list):
        return any(has_template(x) for x in v)
    return False


def evaluate(expr: str, ctx: dict) -> Any:
    return ENV.compile_expression(expr, undefined_to_none=False)(**ctx)


def render(value: Any, ctx: dict) -> Any:
    if isinstance(value, str):
        m = WHOLE.match(value)
        if m and "{{" not in m.group(1) and "}}" not in m.group(1):
            return evaluate(m.group(1).strip(), ctx)
        if "{{" in value or "{%" in value:
            return ENV.from_string(value).render(**ctx)
        return value
    if isinstance(value, dict):
        return {k: render(v, ctx) for k, v in value.items()}
    if isinstance(value, list):
        return [render(v, ctx) for v in value]
    return value
