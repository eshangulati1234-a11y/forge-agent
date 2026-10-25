"""System 1: Laya non-autoregressive decisions (choice / score / yes-no) over grounded options.

Facts verified against laya 0.4.15 on this machine:
  * Router.predict(state, questions, model=...) ; Router(default=...) does NOT force a checkpoint (it routes
    by detected language), so the checkpoint is passed per call.
  * ~45 ms warm per decision on the RTX 3070 Laptop, ~1.6 GB VRAM for the English checkpoint.
  * Shipped probabilities are not calibrated. We never treat them as probabilities: the accept/verify
    thresholds come from config and are refit by `forge bench desktop` on labelled outcomes.
"""
from __future__ import annotations

import asyncio
import logging
import re
import threading
import time
import warnings
from dataclasses import dataclass, field

from forge.config import LayaConfig

log = logging.getLogger("forge.laya")
MAX_OPTIONS = 20  # option-token budget of the checkpoint; above this Laya trims silently


@dataclass
class LayaDecision:
    label: str | None
    top_p: float                 # raw top probability (uncalibrated)
    margin: float                # top - second
    confident: bool              # passed the calibrated threshold
    probabilities: dict[str, float] = field(default_factory=dict)
    latency_ms: float = 0.0
    model: str = ""
    shortlisted_from: int = 0


def _slug(text: str, n: int = 28) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return s[:n] or "option"


def lexical_prerank(goal: str, options: dict[str, str], k: int) -> dict[str, str]:
    words = {w for w in re.findall(r"[a-z0-9]+", goal.lower()) if len(w) > 2}
    def s(item):
        hay = item[1].lower()
        return sum(w in hay for w in words)
    return dict(sorted(options.items(), key=s, reverse=True)[:k])


class LayaEngine:
    def __init__(self, cfg: LayaConfig):
        self.cfg = cfg
        self._router = None
        self._lock = threading.Lock()
        self.calls = 0

    @property
    def loaded(self) -> bool:
        return self._router is not None

    def _ensure(self):
        if self._router is None:
            warnings.filterwarnings("ignore", module="laya")
            warnings.filterwarnings("ignore", message=".*symlinks.*")
            from laya import Router
            t = time.time()
            self._router = Router(max_loaded=1, device=self.cfg.device)
            log.info("laya router ready in %.1fs", time.time() - t)
        return self._router

    def _predict(self, state: str, questions: dict) -> dict:
        with self._lock:
            r = self._ensure()
            self.calls += 1
            return r.predict(state, questions, model=self.cfg.checkpoint)

    # ---- typed operations -------------------------------------------------------------
    def choose_sync(self, state: str, instructions: str, options: dict[str, str], *, goal: str = "",
                    threshold: float | None = None) -> LayaDecision:
        """options: {label: description}. Labels should be meaningful slugs (Laya reads them)."""
        if not options:
            return LayaDecision(None, 0.0, 0.0, False)
        n_in = len(options)
        if n_in > MAX_OPTIONS:
            options = lexical_prerank(goal or instructions, options, MAX_OPTIONS)
        t = time.time()
        res = self._predict(state, {"q": {"type": "choice", "instructions": instructions, "criteria": options}})
        a = res["answers"]["q"]
        probs = {k: float(v) for k, v in a.get("probabilities", {}).items()}
        ranked = sorted(probs.values(), reverse=True) + [0.0, 0.0]
        thr = self.cfg.accept_threshold if threshold is None else threshold
        return LayaDecision(a.get("choice"), ranked[0], ranked[0] - ranked[1], ranked[0] >= thr, probs,
                            (time.time() - t) * 1000, res.get("routing", {}).get("model", ""), n_in)

    def yes_no_sync(self, state: str, question: str, threshold: float | None = None) -> LayaDecision:
        t = time.time()
        res = self._predict(state, {"q": {"type": "noul", "instructions": question}})
        p = float(res["answers"]["q"]["noul"])
        thr = self.cfg.verify_threshold if threshold is None else threshold
        conf = max(p, 1 - p)
        return LayaDecision("yes" if p >= 0.5 else "no", p, abs(2 * p - 1), conf >= thr, {"yes": p, "no": 1 - p},
                            (time.time() - t) * 1000)

    def score_sync(self, state: str, instructions: str, levels: list[str]) -> LayaDecision:
        """Ordinal score. Laya returns the expected level index in [0, n-1]; top_p holds it normalised to [0,1]."""
        t = time.time()
        res = self._predict(state, {"q": {"type": "score", "instructions": instructions, "criteria": levels}})
        a = res["answers"]["q"]
        probs = {levels[int(k)]: float(v) for k, v in a.get("probabilities", {}).items()}
        norm = float(a.get("score", 0.0)) / max(1, len(levels) - 1)
        best = max(probs, key=probs.get) if probs else None
        return LayaDecision(best, min(1.0, max(0.0, norm)), 0.0, float(a.get("answer_confidence", 0)) >= self.cfg.accept_threshold,
                            probs, (time.time() - t) * 1000)

    async def choose(self, *a, **kw) -> LayaDecision:
        return await asyncio.to_thread(self.choose_sync, *a, **kw)

    async def yes_no(self, *a, **kw) -> LayaDecision:
        return await asyncio.to_thread(self.yes_no_sync, *a, **kw)

    async def score(self, *a, **kw) -> LayaDecision:
        return await asyncio.to_thread(self.score_sync, *a, **kw)

    def unload(self) -> None:
        with self._lock:
            self._router = None
            try:
                import torch
                torch.cuda.empty_cache()
            except Exception:
                pass


def options_from_labels(items: list[str]) -> dict[str, str]:
    """Build unique meaningful labels from descriptions."""
    out: dict[str, str] = {}
    for i, desc in enumerate(items):
        base = _slug(desc)
        label = base if base not in out else f"{base}_{i}"
        out[label] = desc
    return out
