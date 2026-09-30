"""STAND-IN judge: deterministic heuristics that mimic the LLM judge's output format.

DEMO / OFFLINE USE ONLY. This is NOT an LLM and its results are NOT evidence of LLM accuracy. It exists so the
pipeline, API and UI can be exercised when OPENAI_MODEL is not configured. It is opt-in (`--standin`), its output
is stamped with model STANDIN_MODEL (so real runs never reuse it) and the dashboard flags it.
"""
from __future__ import annotations

import hashlib
import re

from engine.config import Config
from engine.context import JudgeContext
from engine.llm_judge import JudgeResult
from engine.schemas import JudgeOutput
from engine.verify import need_relevant

STANDIN_MODEL = "stand-in-rules-v1"
RELATIVE = re.compile(r"\b(helping|for|on behalf of)\s+my\s+(mother|father|mum|mom|dad|parents?|friend|"
                      r"sister|brother|grand\w+|neighbou?r)\b", re.I)
SENSITIVE = re.compile(r"passed away|has died|funeral|divorc|cancer|diagnos|bereave", re.I)
STRESS = re.compile(r"short this month|can't afford|cannot afford|split the|behind on|struggling", re.I)


def _plain(name: str) -> str:
    return name.replace("_", " ")


def standin_judge(ctx: JudgeContext, config: Config) -> JudgeResult:
    h = hashlib.sha256(ctx.user_message().encode()).hexdigest()
    kate = [k["text"] for k in ctx.payload["kate_messages"]]
    text = " ".join(kate)

    by_event: dict[str, list] = {}
    for s in ctx.signals:
        by_event.setdefault(s.event_type, []).append(s)
    event = max(by_event, key=lambda e: (sum(2 if s.strength == "strong" else 1 for s in by_event[e]), e),
                default="none")
    sigs = by_event.get(event, [])
    strong = [s for s in sigs if s.strength == "strong"]

    def none(reason: str) -> JudgeResult:
        out = JudgeOutput(life_event="none", stage="none", confidence=0.1, evidence=[], needs=[],
                          top_action="none", suggested_channel="none", urgency="low", message="",
                          why="We noticed some activity but it does not point to a life event.",
                          financial_stress_suspected=False, sensitive=False, rejection_reason=reason)
        return JudgeResult(out, h, STANDIN_MODEL)

    if SENSITIVE.search(text):
        ev = [{"ref_id": s.ref_id, "note": _plain(s.name)} for s in sigs[:3]] or \
             [{"ref_id": ctx.payload["kate_messages"][0]["ref_id"], "note": "message to Kate"}]
        out = JudgeOutput.model_validate(dict(
            life_event="sensitive_other", stage="possible", confidence=0.9, evidence=ev, needs=[],
            top_action="none", suggested_channel="human_support", urgency="medium", message="",
            why="We noticed a message that may call for personal help.",
            financial_stress_suspected=False, sensitive=True, rejection_reason=None))
        return JudgeResult(out, h, STANDIN_MODEL)
    if RELATIVE.search(text):
        return none("the signals describe a relative's situation, not the customer's")
    if not strong:
        return none("purchases alone do not show a life event" if sigs else "no relevant signals")

    n = len(strong)
    conf = 0.45 if n == 1 else 0.6 if n == 2 else 0.85
    if any(s.strength == "medium" for s in sigs) and n >= 3:
        conf = 0.92
    catalog = config.need_catalog(event)
    names = {s.name for s in ctx.signals}
    ordered = [n_ for n_, c in catalog.items() if not c.get("sales") and need_relevant(c["relevant_if"], ctx)]
    ordered.sort(key=lambda n_: (not catalog[n_].get("urgency_boost"), list(catalog).index(n_)))
    if "new_energy_contract" in names and "utilities_setup" in ordered:
        ordered.remove("utilities_setup")
    top = ordered[0]
    stress = bool(STRESS.search(text))
    evidence = [{"ref_id": s.ref_id, "note": _plain(s.name)} for s in strong[:4]]
    why = "We noticed " + ", ".join(_plain(s.name) for s in strong[:4]) + "."
    title = catalog[top]["title"]
    out = JudgeOutput.model_validate(dict(
        life_event=event, stage="in_progress" if n >= 3 else "possible", confidence=conf, evidence=evidence,
        needs=[{"need_id": n_, "priority": i + 1, "reason": catalog[n_]["title"]} for i, n_ in enumerate(ordered)],
        top_action=top, suggested_channel="kate_message", urgency="medium",
        message=f"{title}? We can prepare it for you, it takes one tap.", why=why,
        financial_stress_suspected=stress, sensitive=False, rejection_reason=None))
    return JudgeResult(out, h, STANDIN_MODEL)
