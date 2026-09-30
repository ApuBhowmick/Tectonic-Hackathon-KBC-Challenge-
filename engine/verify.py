"""Evidence verification and catalog checks on the judge output (TECH_SPEC 7). Pure functions."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from engine.config import Config
from engine.context import JudgeContext
from engine.schemas import Evidence, JudgeOutput, Need

MAX_MESSAGE = 280


@dataclass(frozen=True)
class Verified:
    output: JudgeOutput
    notes: list[str] = field(default_factory=list)   # e.g. evidence_unverified, dropped_ref:<id>


def need_relevant(relevant_if: str, ctx: JudgeContext) -> bool:
    """Evaluate the small set of `relevant_if` forms used in life_events.yaml. Unknown forms are relevant."""
    expr = str(relevant_if).strip()
    if expr == "always":
        return True
    signal_names = {s.name for s in ctx.signals}
    parts = [p.strip() for p in re.split(r"\s+or\s+", expr)]
    results: list[bool] = []
    for p in parts:
        if m := re.fullmatch(r"'([^']+)' in products_held", p):
            results.append(m.group(1) in ctx.profile.products_held)
        elif m := re.fullmatch(r"no (\w+) signal yet", p):
            results.append(m.group(1) not in signal_names)
        elif m := re.fullmatch(r"net_cashflow_30d < (-?\d+(?:\.\d+)?)", p):
            results.append(ctx.net_cashflow_30d < float(m.group(1)))
        elif m := re.fullmatch(r"income_band == (\w+)", p):
            results.append(ctx.profile.income_band == m.group(1))
        else:
            return True
    return any(results)


def verify(out: JudgeOutput, ctx: JudgeContext, config: Config) -> Verified:
    notes: list[str] = []
    confidence = min(1.0, max(0.0, float(out.confidence)))
    if confidence != out.confidence:
        notes.append("confidence_clamped")

    # evidence: every ref_id must exist in this customer's context
    evidence: list[Evidence] = []
    for e in out.evidence:
        if e.ref_id in ctx.ref_ids:
            evidence.append(e)
        else:
            notes.append(f"dropped_ref:{e.ref_id}")
    if out.life_event not in ("none",) and not evidence:
        confidence = min(confidence, 0.3)
        notes.append("evidence_unverified")

    # needs: must be in the catalog of the detected event and relevant for this customer
    catalog = config.need_catalog(out.life_event)
    needs: list[Need] = []
    for n in sorted(out.needs, key=lambda x: x.priority):
        if n.need_id not in catalog:
            notes.append(f"dropped_need:{n.need_id}")
        elif not need_relevant(catalog[n.need_id]["relevant_if"], ctx):
            notes.append(f"irrelevant_need:{n.need_id}")
        else:
            needs.append(n)

    top = out.top_action
    if top != "none" and (top not in catalog or not need_relevant(catalog[top]["relevant_if"], ctx)):
        notes.append(f"invalid_top_action:{top}")
        sales = {n for n, c in catalog.items() if c.get("sales")}
        safe = [n.need_id for n in needs if n.need_id not in sales]      # never fall back to a sales need
        top = safe[0] if safe else "none"
        notes.append(f"top_action_fallback:{top}")

    message = out.message
    if len(message) > MAX_MESSAGE:
        message = message[:MAX_MESSAGE - 1].rstrip() + "…"
        notes.append("message_truncated")
    if out.life_event != "none" and not out.why.startswith("We noticed"):
        notes.append("why_format")

    fixed = out.model_copy(update={"confidence": confidence, "evidence": evidence, "needs": needs,
                                   "top_action": top, "message": message})
    return Verified(fixed, notes)
