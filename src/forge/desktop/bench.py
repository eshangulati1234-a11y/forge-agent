"""Desktop benchmark: end-to-end task completion for three modes on identical tasks.

    uia   deterministic replay of stored selectors (no model)
    laya  Laya System 1 with LLM escalation on low confidence (Forge default)
    llm   LLM chooses every action (generative baseline)

Each task has substeps with a ground-truth control (to measure wrong-action rate) and a final deterministic success
check (to measure task success). Reports per mode: success rate, wrong-action rate, recovery rate, decision /
observation / action latency, total duration, escalations, peak GPU memory. Results are also written as labelled
Laya decisions to FORGE_HOME/bench/laya_records.jsonl, which `forge bench laya` uses to refit the confidence gate.
"""
from __future__ import annotations

import asyncio
import json
import os
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from forge.desktop import execute as ex
from forge.desktop.loop import DesktopAgent
from forge.desktop.perceive import Observation, Selector, find_window

FORM_PS1 = Path(__file__).with_name("testapp") / "form.ps1"


@dataclass
class Sub:
    instruction: str
    expect: str                     # automation_id or name of the correct control
    control_type: str = "Button"
    text: str | None = None
    action: str = "click"


@dataclass
class Task:
    name: str
    goal: str
    window: str
    launch: Callable[[], dict]
    steps: list[Sub]
    check: Callable[[dict], bool]
    teardown: Callable[[dict], None]


def _calc_display() -> str:
    w = find_window("Calculator")
    return w.child_window(auto_id="CalculatorResults", control_type="Text").wrapper_object().window_text()


def _calc_launch() -> dict:
    ex.close_window("Calculator")
    subprocess.Popen(["calc.exe"])
    find_window("Calculator", timeout=15)
    time.sleep(0.8)
    ex.act(Selector(window="Calculator", control_type="Button", automation_id="clearButton"), "click")
    return {}


def _calc_task(name, a, op_name, op_aid, b, result, digits) -> Task:
    words = {0: "Zero", 1: "One", 2: "Two", 3: "Three", 4: "Four", 5: "Five", 6: "Six", 7: "Seven", 8: "Eight", 9: "Nine"}
    return Task(name, f"Use Calculator to compute {a} {op_name} {b}", "Calculator", _calc_launch,
                [Sub(f"press the {a} key", f"num{a}Button"), Sub(f"press the {op_name} operator", op_aid),
                 Sub(f"press the {b} key", f"num{b}Button"), Sub("press equals to get the result", "equalButton")],
                lambda st: _calc_display().endswith(f" {result}"), lambda st: ex.close_window("Calculator"))


def _form_launch(title: str) -> Callable[[], dict]:
    def go() -> dict:
        out = Path(tempfile.mkdtemp()) / "form.json"
        env = {**os.environ, "FORGE_FORM_OUT": str(out)}
        p = subprocess.Popen(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(FORM_PS1), "-Title", title],
                             env=env, creationflags=0x08000000)
        find_window(title, timeout=20)
        time.sleep(0.5)
        return {"proc": p, "out": out}
    return go


def _form_teardown(st: dict) -> None:
    p = st.get("proc")
    if p:
        p.kill()


def _form_check(expect: dict) -> Callable[[dict], bool]:
    def check(st: dict) -> bool:
        try:
            got = json.loads(st["out"].read_text(encoding="utf-8-sig"))
        except Exception:
            return False
        return all(got.get(k) == v for k, v in expect.items())
    return check


def tasks() -> list[Task]:
    t1 = "Forge Test Form - Application"
    t2 = "Forge Test Form - Draft"
    return [
        _calc_task("calc_add", 7, "plus", "plusButton", 5, 12, 2),
        _calc_task("calc_multiply", 9, "multiply by", "multiplyButton", 8, 72, 2),
        _calc_task("calc_subtract", 3, "minus", "minusButton", 1, 2, 1),
        Task("form_fill_save", "Fill in the application form for Ada Lovelace and save it as a draft", t1, _form_launch(t1),
             [Sub("enter the full name", "Full name", "Edit", "Ada Lovelace", "type"),
              Sub("enter the email address", "Email address", "Edit", "ada@example.com", "type"),
              Sub("enter the phone number", "Phone number", "Edit", "0400 000 000", "type"),
              Sub("tick the box confirming the details are true", "Confirm", "CheckBox", action="toggle"),
              Sub("save the form as a draft", "Savedraft")],
             _form_check({"action": "saved_draft", "name": "Ada Lovelace", "email": "ada@example.com", "confirmed": True}), _form_teardown),
        Task("form_work_rights", "Record that the applicant has Australian work rights, then save a draft", t2, _form_launch(t2),
             [Sub("tick the Australian work rights box", "WorkRights", "CheckBox", action="toggle"),
              Sub("save the form as a draft", "Savedraft")],
             _form_check({"action": "saved_draft", "work_rights": True, "confirmed": False}), _form_teardown),
    ]


def _match(chosen: str | None, expect: str) -> bool:
    """WinForms exposes Control.Name as the automation id and AccessibleName as the name; accept either."""
    norm = lambda x: (x or "").replace(" ", "").lower()
    return norm(chosen) == norm(expect)


@dataclass
class Result:
    task: str
    mode: str
    success: bool
    seconds: float
    steps: int
    wrong_actions: int = 0
    recovered: int = 0
    escalations: int = 0
    decision_ms: list[float] = field(default_factory=list)
    observe_ms: list[float] = field(default_factory=list)
    act_ms: list[float] = field(default_factory=list)
    error: str = ""


class GpuSampler(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.peak = 0
        self._stop = threading.Event()

    def run(self):
        while not self._stop.is_set():
            try:
                out = subprocess.run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"], capture_output=True,
                                     text=True, timeout=3, creationflags=0x08000000).stdout
                self.peak = max(self.peak, int(out.strip()))
            except Exception:
                pass
            self._stop.wait(0.5)

    def stop(self):
        self._stop.set()


async def run_task(services, task: Task, mode: str, records: list) -> Result:
    st = task.launch()
    t0 = time.time()
    res = Result(task.name, mode, False, 0, len(task.steps))
    try:
        if mode == "uia":
            for s in task.steps:
                sel = Selector(window=task.window, control_type=s.control_type,
                               automation_id=s.expect if s.control_type != "Edit" else None,
                               name=s.expect if s.control_type == "Edit" else None)
                r = await asyncio.to_thread(ex.act, sel, s.action, s.text)
                res.act_ms.append(r.ms)
        else:
            agent = DesktopAgent(services, mode=mode)
            for s in task.steps:
                tr = await agent.do_step(task.goal, s.instruction, task.window, text=s.text)
                first_ok = bool(tr.chosen) and _match(tr.chosen[0], s.expect)
                res.wrong_actions += sum(1 for c in tr.chosen if not _match(c, s.expect))
                res.recovered += int(tr.verified and not first_ok)
                res.escalations += tr.escalations
                res.decision_ms += tr.decision_ms
                res.observe_ms += tr.observe_ms
                res.act_ms += tr.act_ms
                if mode == "laya":
                    for lc, conf in zip(tr.laya_choice, tr.laya_conf):
                        if conf is not None:
                            records.append({"task": task.name, "step": s.instruction, "laya_choice": lc, "confidence": conf,
                                            "correct": _match(lc, s.expect)})
                if not tr.verified:
                    res.error = f"step failed: {s.instruction} (chose {tr.chosen})"
                    break
        await asyncio.sleep(0.4)
        res.success = await asyncio.to_thread(task.check, st)
    except Exception as e:
        res.error = f"{type(e).__name__}: {e}"[:300]
    finally:
        res.seconds = time.time() - t0
        await asyncio.to_thread(task.teardown, st)
        await asyncio.sleep(0.5)
    return res


def _p50(xs):
    return round(statistics.median(xs), 1) if xs else None


def summarise(results: list[Result], peaks: dict) -> dict:
    out = {}
    for mode in sorted({r.mode for r in results}):
        rs = [r for r in results if r.mode == mode]
        decisions = sum(len(r.decision_ms) for r in rs) or None
        out[mode] = {
            "tasks": len(rs), "success_rate": round(sum(r.success for r in rs) / len(rs), 3),
            "wrong_action_rate": round(sum(r.wrong_actions for r in rs) / decisions, 3) if decisions else 0.0,
            "recoveries": sum(r.recovered for r in rs), "escalations": sum(r.escalations for r in rs),
            "escalation_rate": round(sum(r.escalations for r in rs) / decisions, 3) if decisions else 0.0,
            "p50_decision_ms": _p50([x for r in rs for x in r.decision_ms]), "p50_observe_ms": _p50([x for r in rs for x in r.observe_ms]),
            "p50_action_ms": _p50([x for r in rs for x in r.act_ms]), "p50_task_s": _p50([r.seconds for r in rs]),
            "total_s": round(sum(r.seconds for r in rs), 1), "peak_gpu_mb": peaks.get(mode),
            "failures": [f"{r.task}: {r.error}" for r in rs if not r.success][:10]}
    return out


async def _main(runs: int, modes: list[str]) -> dict:
    from forge.services import Services
    s = Services()
    s.cfg.notify_toast = False
    await s.router.refresh()
    results: list[Result] = []
    records: list = []
    peaks = {}
    for mode in modes:
        g = GpuSampler()
        g.start()
        for _ in range(runs):
            for t in tasks():
                r = await run_task(s, t, mode, records)
                print(f"[{mode:4s}] {t.name:18s} {'OK ' if r.success else 'FAIL'} {r.seconds:5.1f}s wrong={r.wrong_actions} esc={r.escalations} {r.error[:100]}",
                      flush=True)
                results.append(r)
        g.stop()
        peaks[mode] = g.peak
    summary = summarise(results, peaks)
    outdir = s.cfg.home / "bench"
    outdir.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    (outdir / f"desktop-{stamp}.json").write_text(json.dumps({"summary": summary, "results": [r.__dict__ for r in results]}, indent=2, default=str))
    with open(outdir / "laya_records.jsonl", "a", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec) + "\n")
    return summary


def main(runs: int = 3, modes: list[str] | None = None) -> None:
    summary = asyncio.run(_main(runs, modes or ["uia", "laya", "llm"]))
    print(json.dumps(summary, indent=2))


def laya_calibration(target_precision: float = 0.95) -> dict:
    """Refit the Laya accept threshold from labelled decisions logged by `forge bench desktop`:
    the lowest threshold whose accepted decisions reach the target precision (everything below escalates)."""
    from forge.config import forge_home
    path = forge_home() / "bench" / "laya_records.jsonl"
    recs = [json.loads(l) for l in path.read_text().splitlines() if l.strip()] if path.exists() else []
    if len(recs) < 20:
        print(f"only {len(recs)} labelled decisions; run `forge bench desktop` more before calibrating")
        return {}
    best = None
    for thr in sorted({round(r["confidence"], 3) for r in recs}):
        acc = [r for r in recs if r["confidence"] >= thr]
        if acc and sum(r["correct"] for r in acc) / len(acc) >= target_precision:
            best = {"threshold": thr, "coverage": round(len(acc) / len(recs), 3),
                    "precision": round(sum(r["correct"] for r in acc) / len(acc), 3)}
            break
    raw_acc = round(sum(r["correct"] for r in recs) / len(recs), 3)
    out = {"n": len(recs), "raw_accuracy": raw_acc, "fit": best}
    print(json.dumps(out, indent=2))
    if best:
        print(f"set [laya] accept_threshold = {best['threshold']} in forge.toml")
    return out


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 1)
