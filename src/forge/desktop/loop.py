"""The desktop decision cycle (System 1 / System 2).

    observe -> candidates -> filter (disabled, chrome, high-risk unless approved, already-failed)
            -> decide (Laya choice over candidate labels; LLM over the same enum when Laya is not confident)
            -> permission check -> act (UIA pattern first) -> re-observe -> verify -> continue | retry | escalate

`mode` selects the decision-maker so the same loop drives the benchmark:
    laya  - Laya first, LLM on low confidence (the Forge default)
    llm   - LLM chooses every action (baseline)
Deterministic replay (`uia`) bypasses this loop entirely: compiled workflows call desktop.act with stored selectors,
and only fall into this loop to repair a selector that no longer resolves.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Callable, Literal

from forge.desktop import execute as ex
from forge.desktop.perceive import ActionCandidate, Observation, Selector, observe
from forge.errors import InterfaceChanged, NeedsApproval


@dataclass
class StepTrace:
    instruction: str
    attempts: int = 0
    chosen: list[str] = field(default_factory=list)
    systems: list[int] = field(default_factory=list)
    decision_ms: list[float] = field(default_factory=list)
    observe_ms: list[float] = field(default_factory=list)
    act_ms: list[float] = field(default_factory=list)
    verify_ms: list[float] = field(default_factory=list)
    methods: list[str] = field(default_factory=list)
    verified: bool = False
    escalations: int = 0
    laya_choice: list[str | None] = field(default_factory=list)   # raw System 1 pick (even when escalated)
    laya_conf: list[float | None] = field(default_factory=list)   # raw, uncalibrated top probability


class DesktopAgent:
    def __init__(self, services, *, mode: Literal["laya", "llm"] = "laya", allow_high_risk: bool = False,
                 approve_high_risk: Callable[[ActionCandidate], bool] | None = None):
        self.s = services
        self.mode = mode
        self.allow_high_risk = allow_high_risk
        self.approve_high_risk = approve_high_risk
        self.history: list[str] = []

    async def _observe(self, window_re: str) -> Observation:
        return await asyncio.to_thread(observe, window_re)

    async def do_step(self, goal: str, instruction: str, window_re: str, *, text: str | None = None,
                      verify: Callable[[Observation], bool] | None = None, max_attempts: int = 3) -> StepTrace:
        tr = StepTrace(instruction)
        excluded: set[str] = set()
        while tr.attempts < max_attempts:
            tr.attempts += 1
            obs = await self._observe(window_re)
            tr.observe_ms.append(obs.observe_ms)
            cands = [c for c in obs.candidates if c.label not in excluded and (self.allow_high_risk or c.risk == "low")]
            if text is not None:
                typable = [c for c in cands if c.action == "type"]
                cands = typable or cands
            if not cands:
                raise InterfaceChanged(f"no actionable controls for step '{instruction}' in /{window_re}/")
            options = {c.label: c.describe() for c in cands}
            state = (f"Goal: {goal}\nCurrent step: {instruction}" + (f" (text to enter: {text!r})" if text else "") +
                     f"\n{obs.summary()}\nRecent actions: {'; '.join(self.history[-3:]) or 'none'}")
            laya_options = {c.label: c.name or c.automation_id for c in cands}
            t0 = time.time()
            d = await self.s.decisions.choose(state, f"Which single action performs the step: {instruction}?", options,
                                              use_laya=self.mode == "laya", goal=instruction,
                                              laya_state=f"Current step: {instruction}", laya_options=laya_options)
            tr.decision_ms.append((time.time() - t0) * 1000)
            tr.systems.append(d["system"])
            if d["system"] == 2 and self.mode == "laya":
                tr.escalations += 1
            cand = next(c for c in cands if c.label == d["choice"])
            tr.chosen.append(cand.automation_id or cand.name)
            lc = d.get("laya_choice", d["choice"] if d["system"] == 1 else None)
            lcand = next((c for c in cands if c.label == lc), None)
            tr.laya_choice.append((lcand.automation_id or lcand.name) if lcand else None)
            tr.laya_conf.append(d.get("laya_confidence", d.get("confidence")))
            if cand.risk == "high" and not self.allow_high_risk:
                if not (self.approve_high_risk and self.approve_high_risk(cand)):
                    raise NeedsApproval(f"high-risk desktop action: {cand.describe()}", approval_id="")
            res = await asyncio.to_thread(ex.act, cand, None, text if cand.action == "type" else None)
            tr.act_ms.append(res.ms)
            tr.methods.append(res.method)
            await asyncio.sleep(0.15)  # let the UI settle
            t1 = time.time()
            after = await self._observe(window_re)
            ok = res.ok and await self._verify(goal, instruction, obs, after, verify, cand, text)
            tr.verify_ms.append((time.time() - t1) * 1000)
            self.history.append(f"{cand.describe()} -> {'ok' if ok else 'no effect'}")
            if ok:
                tr.verified = True
                return tr
            excluded.add(cand.label)
        return tr

    async def _verify(self, goal, instruction, before: Observation, after: Observation, verify,
                      cand: ActionCandidate | None = None, text: str | None = None) -> bool:
        """Deterministic checks first (value read-back, toggle state); a model is asked only when the UI offers no
        checkable property for the action."""
        if verify is not None:
            return bool(verify(after))
        if cand is not None and cand.action == "type" and text is not None:
            try:
                return (await asyncio.to_thread(ex.read_text, cand.selector)).strip() == text.strip()
            except Exception:
                pass
        if cand is not None and cand.action == "toggle":
            now_c = next((c for c in after.candidates if c.selector == cand.selector), None)
            if now_c is not None and now_c.value is not None and cand.value is not None:
                return now_c.value != cand.value
        if after.digest == before.digest:
            return False
        state = f"Goal: {goal}\nStep just attempted: {instruction}\nState after the action:\n{after.summary(20)}"
        q = f"Is the step '{instruction}' now completed?"
        changed = sorted(set(after.texts) - set(before.texts))[:8]
        laya_state = f"Step: {instruction}. Last action: {self.history[-1] if self.history else ''}. Screen text now: {' | '.join(changed) or 'unchanged'}"
        d = await self.s.decisions.yes_no(state, q, use_laya=self.mode == "laya", laya_state=laya_state)
        return bool(d["answer"])

    async def replay(self, selector: Selector, action: str, text: str | None = None, *, goal: str = "") -> dict:
        """Deterministic step with Laya-based self-healing when the stored selector no longer resolves."""
        try:
            r = await asyncio.to_thread(ex.act, selector, action, text)
            return {"ok": r.ok, "method": r.method, "healed": False}
        except InterfaceChanged:
            tr = await self.do_step(goal or f"{action} {selector.describe()}", f"{action} the control previously known as "
                                    f"'{selector.name or selector.automation_id}' ({selector.control_type})", selector.window, text=text)
            return {"ok": tr.verified, "healed": True, "chosen": tr.chosen[-1] if tr.chosen else None, "systems": tr.systems}
