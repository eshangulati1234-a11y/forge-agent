"""`decide` steps: System 1 (Laya) with escalation to System 2 (LLM, constrained to the same options).

The LLM fallback uses a JSON schema whose enum is exactly the option labels, so neither system can pick an
option that does not exist. Every decision emits a `decision` event used by benchmarks/calibration.
"""
from __future__ import annotations

from typing import Any

from forge.models.laya import options_from_labels
from forge.workflows.template import render


class Decisions:
    def __init__(self, services):
        self.s = services

    async def decide(self, step, ctx: dict, mocks: dict | None = None) -> dict:
        state = str(render(step.state, ctx))
        question = str(render(step.question, ctx))
        laya_on = self.s.cfg.laya.enabled and self.s.laya is not None and not (mocks and "laya" in mocks)
        if step.mode == "choice":
            raw = render(step.options, ctx)
            options = raw if isinstance(raw, dict) else options_from_labels([str(x) for x in raw])
            return await self.choose(state, question, {str(k): str(v) for k, v in options.items()},
                                     threshold=step.threshold, escalate=step.escalate, use_laya=laya_on)
        if step.mode == "yes_no":
            return await self.yes_no(state, question, threshold=step.threshold, escalate=step.escalate, use_laya=laya_on)
        return await self.score(state, question, step.levels or [], escalate=step.escalate, use_laya=laya_on)

    async def choose(self, state: str, question: str, options: dict[str, str], *, threshold=None, escalate=True,
                     use_laya=True, goal: str = "", laya_state: str | None = None, laya_options: dict[str, str] | None = None) -> dict:
        """laya_state / laya_options: Laya-specific framing. Measured on Calculator: putting the full control list in
        Laya's state dropped accuracy from 7/8 to 0/8, so System 1 gets a minimal state and short option texts
        while System 2 gets the full observation."""
        out: dict[str, Any] = {"options": len(options)}
        if use_laya:
            d = await self.s.laya.choose(laya_state or state, question, laya_options or options, goal=goal, threshold=threshold)
            out.update(choice=d.label, confidence=round(d.top_p, 4), margin=round(d.margin, 4), system=1, laya_ms=round(d.latency_ms, 1))
            if d.confident or not escalate:
                self._emit("choice", out)
                return out
            out.update(laya_choice=d.label, laya_confidence=round(d.top_p, 4))
        labels = list(options)
        schema = {"type": "object", "properties": {"choice": {"type": "string", "enum": labels}, "reason": {"type": "string"}},
                  "required": ["choice", "reason"]}
        listing = "\n".join(f"- {k}: {v}" for k, v in options.items())
        res = await self.s.router.generate("reasoning", [
            {"role": "system", "content": "You choose exactly one option label. Reply with JSON only."},
            {"role": "user", "content": f"Situation:\n{state}\n\nQuestion: {question}\n\nOptions:\n{listing}"}],
            schema=schema, temperature=0.0, max_tokens=300)
        data = res.json()
        out.update(choice=data["choice"], reason=data.get("reason", ""), system=2, model=res.model, llm_ms=res.duration_ms)
        self._emit("choice", out)
        return out

    async def yes_no(self, state: str, question: str, *, threshold=None, escalate=True, use_laya=True,
                     laya_state: str | None = None) -> dict:
        out: dict[str, Any] = {}
        if use_laya:
            d = await self.s.laya.yes_no(laya_state or state, question, threshold=threshold)
            out.update(answer=d.label == "yes", p_yes=round(d.top_p, 4), system=1, laya_ms=round(d.latency_ms, 1))
            if d.confident or not escalate:
                self._emit("yes_no", out)
                return out
            out["laya_p_yes"] = out["p_yes"]
        schema = {"type": "object", "properties": {"answer": {"type": "boolean"}, "reason": {"type": "string"}}, "required": ["answer", "reason"]}
        res = await self.s.router.generate("reasoning", [
            {"role": "system", "content": "Answer the yes/no question strictly from the situation. Reply with JSON only."},
            {"role": "user", "content": f"Situation:\n{state}\n\nQuestion: {question}"}], schema=schema, temperature=0.0, max_tokens=300)
        data = res.json()
        out.update(answer=bool(data["answer"]), reason=data.get("reason", ""), system=2, model=res.model)
        self._emit("yes_no", out)
        return out

    async def score(self, state: str, question: str, levels: list[str], *, escalate=True, use_laya=True) -> dict:
        out: dict[str, Any] = {"levels": levels}
        if use_laya:
            d = await self.s.laya.score(state, question, levels)
            out.update(score=round(d.top_p, 4), level=d.label, system=1, laya_ms=round(d.latency_ms, 1))
            if d.confident or not escalate:
                self._emit("score", out)
                return out
            out.update(laya_score=out["score"], laya_level=d.label)
        schema = {"type": "object", "properties": {"level": {"type": "string", "enum": levels}, "reason": {"type": "string"}},
                  "required": ["level", "reason"]}
        res = await self.s.router.generate("reasoning", [
            {"role": "system", "content": "Pick the single best-matching level. Reply with JSON only."},
            {"role": "user", "content": f"Situation:\n{state}\n\nQuestion: {question}\nLevels (worst to best): {levels}"}],
            schema=schema, temperature=0.0, max_tokens=300)
        data = res.json()
        idx = levels.index(data["level"])
        out.update(level=data["level"], score=round(idx / max(1, len(levels) - 1), 4), reason=data.get("reason", ""), system=2, model=res.model)
        self._emit("score", out)
        return out

    def _emit(self, mode: str, out: dict) -> None:
        self.s.events.emit("decision", mode=mode, system=out.get("system"), confidence=out.get("confidence") or out.get("p_yes"))
