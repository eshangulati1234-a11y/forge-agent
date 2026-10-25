"""Typed workflow IR. YAML/JSON on disk, validated by pydantic, statically checked by `check_ir`.

A deployed workflow is deterministic: tool steps run without any model; `llm` and `decide` steps are
explicit, schema-constrained and visible in the graph, so model use is selective by construction.
"""
from __future__ import annotations

import re
from typing import Annotated, Any, Literal, Union

import yaml
from jinja2 import TemplateSyntaxError
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from forge.policy.engine import Permissions

ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,40}$")


class RetryPolicy(BaseModel):
    max_attempts: int = Field(3, ge=1, le=10)
    backoff_s: float = Field(2.0, ge=0)
    factor: float = Field(2.0, ge=1)
    max_backoff_s: float = 60.0


class Trigger(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["manual", "cron", "interval", "file", "webhook"]
    cron: str | None = None              # "0 8 * * *" (local time)
    every_s: int | None = Field(None, ge=10)
    path: str | None = None              # file trigger directory
    pattern: str = "*"
    inputs: dict[str, Any] = Field(default_factory=dict)


class InputSpec(BaseModel):
    type: Literal["string", "number", "integer", "boolean", "array", "object"] = "string"
    description: str = ""
    default: Any = None
    required: bool = False


class _Step(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    id: str
    description: str = ""
    when: str | None = None              # jinja expression; step skipped when falsy
    retry: RetryPolicy | None = None
    timeout_s: float = Field(300, gt=0)
    continue_on_error: bool = False
    idempotency_key: str | None = None   # template; required to auto-retry external tools

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        if not ID_RE.match(v):
            raise ValueError("step id must be lower_snake_case, start with a letter, <= 41 chars")
        return v


class ToolStep(_Step):
    kind: Literal["tool"] = "tool"
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)


class LlmStep(_Step):
    kind: Literal["llm"] = "llm"
    prompt: str
    system: str | None = None
    output_schema: dict | None = Field(None, alias="schema")
    capability: str = "extraction"
    complexity: Literal["low", "normal", "high"] = "normal"
    max_tokens: int = 2048


class DecideStep(_Step):
    """System 1 decision via Laya, with optional escalation to an LLM when not confident."""
    kind: Literal["decide"] = "decide"
    mode: Literal["choice", "score", "yes_no"]
    state: str                           # template describing the situation
    question: str
    options: Any = None                  # choice: {label: description} or template -> dict/list
    levels: list[str] | None = None      # score levels, worst -> best
    threshold: float | None = None
    escalate: bool = True


class SetStep(_Step):
    kind: Literal["set"] = "set"
    values: dict[str, Any]


class ApprovalStep(_Step):
    kind: Literal["approval"] = "approval"
    title: str
    details: str = ""
    ttl_s: float = 7 * 24 * 3600


class BranchStep(_Step):
    kind: Literal["branch"] = "branch"
    condition: str
    then: list["Step"] = Field(default_factory=list)
    otherwise: list["Step"] = Field(default_factory=list, alias="else")


class ForeachStep(_Step):
    kind: Literal["foreach"] = "foreach"
    items: str                           # expression -> list
    as_: str = Field("item", alias="as")
    steps: list["Step"]
    max_parallel: int = Field(1, ge=1, le=8)
    max_items: int = Field(500, ge=1)


class ParallelStep(_Step):
    kind: Literal["parallel"] = "parallel"
    branches: dict[str, list["Step"]]


Step = Annotated[Union[ToolStep, LlmStep, DecideStep, SetStep, ApprovalStep, BranchStep, ForeachStep, ParallelStep],
                 Field(discriminator="kind")]
for _m in (BranchStep, ForeachStep, ParallelStep):
    _m.model_rebuild()


class WorkflowTest(BaseModel):
    name: str
    inputs: dict[str, Any] = Field(default_factory=dict)
    mocks: dict[str, Any] = Field(default_factory=dict, description="tool name -> canned output during the test")
    expect: list[str] = Field(default_factory=list, description="jinja expressions that must be truthy")


class WorkflowIR(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str                              # dotted slug, e.g. careerpilot.job_watch
    name: str
    description: str = ""
    triggers: list[Trigger] = Field(default_factory=lambda: [Trigger(type="manual")])
    inputs: dict[str, InputSpec] = Field(default_factory=dict)
    permissions: Permissions = Field(default_factory=Permissions)
    steps: list[Step]
    outputs: dict[str, str] = Field(default_factory=dict)
    tests: list[WorkflowTest] = Field(default_factory=list)

    @field_validator("id")
    @classmethod
    def _wid(cls, v: str) -> str:
        if not re.match(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$", v):
            raise ValueError("workflow id must be dotted lower_snake_case, e.g. careerpilot.job_watch")
        return v

    @classmethod
    def from_yaml(cls, text: str) -> "WorkflowIR":
        return cls.model_validate(yaml.safe_load(text))

    def to_yaml(self) -> str:
        return yaml.safe_dump(self.model_dump(mode="json", by_alias=True, exclude_none=True), sort_keys=False, allow_unicode=True)


# ---------------------------------------------------------------------------------------
# Static checks (compile errors). Returned as structured issues so the compiler can feed them back to the LLM.

class Issue(BaseModel):
    path: str
    code: str
    message: str


def walk(steps: list, prefix: str = ""):
    for s in steps:
        yield prefix + s.id, s
        if isinstance(s, BranchStep):
            yield from walk(s.then, f"{prefix}{s.id}.then.")
            yield from walk(s.otherwise, f"{prefix}{s.id}.else.")
        elif isinstance(s, ForeachStep):
            yield from walk(s.steps, f"{prefix}{s.id}[*].")
        elif isinstance(s, ParallelStep):
            for b, bs in s.branches.items():
                yield from walk(bs, f"{prefix}{s.id}.{b}.")


STEP_REF = re.compile(r"steps\.([a-z][a-z0-9_]*)")


def check_ir(ir: WorkflowIR, registry) -> list[Issue]:
    from forge.workflows.template import ENV, has_template
    issues: list[Issue] = []
    seen: set[str] = set()
    all_ids = {s.id for _, s in walk(ir.steps)}

    def templates_of(s) -> list[str]:
        out = [s.when] if s.when else []
        if isinstance(s, ToolStep):
            out += [v for v in _strings(s.args)]
        elif isinstance(s, LlmStep):
            out += [s.prompt] + ([s.system] if s.system else [])
        elif isinstance(s, DecideStep):
            out += [s.state, s.question] + list(_strings(s.options))
        elif isinstance(s, SetStep):
            out += list(_strings(s.values))
        elif isinstance(s, BranchStep):
            out.append("{{ " + s.condition + " }}")
        elif isinstance(s, ForeachStep):
            out.append("{{ " + s.items + " }}")
        return out

    for path, s in walk(ir.steps):
        if s.id in seen and "[*]" not in path:
            issues.append(Issue(path=path, code="duplicate_id", message=f"step id '{s.id}' is used twice"))
        seen.add(s.id)
        for t in templates_of(s):
            if not isinstance(t, str):
                continue
            try:
                ENV.parse(t)
            except TemplateSyntaxError as e:
                issues.append(Issue(path=path, code="template_syntax", message=f"{e.message} in {t[:80]!r}"))
            for ref in STEP_REF.findall(t):
                if ref not in all_ids:
                    issues.append(Issue(path=path, code="unknown_step_ref", message=f"references steps.{ref}, which does not exist"))
        if isinstance(s, ToolStep):
            spec = registry.get(s.tool)
            if not spec:
                close = [n for n in registry.names() if n.split(".")[0] == s.tool.split(".")[0]]
                issues.append(Issue(path=path, code="unknown_tool",
                                    message=f"tool '{s.tool}' does not exist" + (f"; did you mean one of {close}?" if close else "")))
                continue
            if not ir.permissions.allows_tool(spec.name):
                issues.append(Issue(path=path, code="tool_not_granted", message=f"tool '{spec.name}' is used but not listed in permissions.tools"))
            for cap in ("desktop", "browser", "shell"):
                if cap in spec.caps and not getattr(ir.permissions, cap):
                    issues.append(Issue(path=path, code="capability_not_granted", message=f"tool '{spec.name}' needs permissions.{cap}: true"))
            # Validate concrete (non-templated) args now; templated ones are validated at run time.
            concrete = {k: v for k, v in s.args.items() if not has_template(v)}
            fields = spec.input_model.model_fields
            for k in s.args:
                if k not in fields:
                    issues.append(Issue(path=path, code="unknown_arg", message=f"tool '{spec.name}' has no argument '{k}'; valid: {sorted(fields)}"))
            for name, f in fields.items():
                if f.is_required() and name not in s.args:
                    issues.append(Issue(path=path, code="missing_arg", message=f"tool '{spec.name}' requires argument '{name}'"))
            try:
                spec.input_model.model_validate({**_placeholders(spec, s.args), **concrete})
            except ValidationError as e:
                for err in e.errors():
                    loc = ".".join(str(p) for p in err["loc"])
                    if loc in concrete:
                        issues.append(Issue(path=path, code="bad_arg", message=f"{spec.name}.{loc}: {err['msg']}"))
        if isinstance(s, DecideStep):
            if s.mode == "choice" and not s.options:
                issues.append(Issue(path=path, code="missing_options", message="choice decisions need options"))
            if s.mode == "score" and not s.levels:
                issues.append(Issue(path=path, code="missing_levels", message="score decisions need levels"))
    for t in ir.triggers:
        if t.type == "cron":
            from croniter import croniter
            if not t.cron or not croniter.is_valid(t.cron):
                issues.append(Issue(path="triggers", code="bad_cron", message=f"invalid cron expression {t.cron!r}"))
        if t.type == "interval" and not t.every_s:
            issues.append(Issue(path="triggers", code="bad_interval", message="interval trigger needs every_s"))
        if t.type == "file" and not t.path:
            issues.append(Issue(path="triggers", code="bad_file_trigger", message="file trigger needs path"))
    return issues


def _strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _strings(v)


def _placeholders(spec, args: dict) -> dict:
    """Fill templated args with type-appropriate dummies so only concrete args produce errors."""
    from forge.workflows.template import has_template
    out = {}
    for k, v in args.items():
        if not has_template(v):
            continue
        f = spec.input_model.model_fields.get(k)
        ann = str(f.annotation) if f else ""
        out[k] = 0 if "int" in ann or "float" in ann else [] if "list" in ann else {} if "dict" in ann else False if "bool" in ann else "x"
    return out
