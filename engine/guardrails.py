"""Guardrails: code decides (TECH_SPEC 8). Pure function, ordered rules, policy from config/policy.yaml.

The LLM only suggests. Contact / wait / suppress, the channel and the allowed needs are decided here.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from engine.config import Config
from engine.features import Profile
from engine.schemas import JudgeOutput

URGENCY_RANK = {"low": 0, "medium": 1, "high": 2}
BUDGET_NEED = "budget_check"
HUMAN_SUPPORT_MESSAGE = ("We are here if you need a hand. Would you like one of our advisors to get in touch "
                         "with you, whenever it suits you?")
HUMAN_SUPPORT_WHY = ("We noticed a message from you that may call for personal help, so we are offering "
                     "to connect you with a person instead of an automated suggestion.")


@dataclass(frozen=True)
class CustomerState:
    profile: Profile
    net_cashflow_30d: float
    adjusted_cashflow_30d: float | None = None  # net cashflow without the detected event's own signal spend
    disabled_categories: frozenset[str] = frozenset()


@dataclass(frozen=True)
class History:
    contacts: tuple[tuple[date, str | None], ...] = ()        # earlier proactive contacts (date, event_type)
    feedback: tuple[tuple[str, str, date], ...] = ()          # (event_type, response, date)


@dataclass(frozen=True)
class FinalDecision:
    action: str                     # contact | wait | suppress
    reason: str | None              # why not contacted, or a contact modifier (sensitive, stress_restricted)
    channel: str | None = None
    top_need: str | None = None
    message: str | None = None
    why: str | None = None
    event_type: str | None = None
    confidence: float | None = None
    stress: bool = False
    notes: list[str] = field(default_factory=list)


def stress_suspected(out: JudgeOutput, state: CustomerState, policy: dict[str, Any]) -> tuple[bool, list[str]]:
    """LLM flag OR (adjusted 30-day cashflow below the limit AND income band in the configured bands)."""
    rules = policy.get("stress_rules", {})
    why: list[str] = []
    if out.financial_stress_suspected:
        why.append("llm_flag")
    below = rules.get("adjusted_net_cashflow_30d_below")
    adj = state.adjusted_cashflow_30d if state.adjusted_cashflow_30d is not None else state.net_cashflow_30d
    if below is not None and adj < below and state.profile.income_band in rules.get("income_bands", []):
        why.append("low_income_negative_cashflow")
    return bool(why), why


def _within_backoff(event_type: str, history: History, as_of: date, policy: dict[str, Any]) -> str | None:
    backoff = policy["backoff_days"]
    for ev, response, d in history.feedback:
        if ev == event_type and response in backoff and 0 <= (as_of - d).days < backoff[response]:
            return "recently_accepted" if response == "accepted" else "recent_dismissal"
    return None


def resolve_channel(suggested: str, urgency: str, state: CustomerState, policy: dict[str, Any]) -> str:
    ch = suggested
    cfg = policy["channels"]
    if ch == "voice" and not (cfg["voice_enabled"] and
                              URGENCY_RANK[urgency] >= URGENCY_RANK[cfg["voice_min_urgency"]]):
        ch = "kate_message"
    if ch == "none":
        ch = "app_card"
    if ch == "kate_message" and "kate" in state.disabled_categories:
        ch = "app_card"                      # no consent to use Kate: downgrade to the in-app card
    return ch


def decide(out: JudgeOutput | None, state: CustomerState, history: History, config: Config,
           as_of: date) -> FinalDecision:
    policy = config.policy
    # 0. no usable LLM output (never crash the pipeline)
    if out is None:
        return FinalDecision("suppress", "llm_unavailable")
    base = dict(event_type=out.life_event, confidence=out.confidence)

    # 1. no event
    if out.life_event == "none":
        return FinalDecision("suppress", "no_event", **base)

    sensitive = out.sensitive or out.life_event in policy["sensitive_event_types"]

    # 3. confidence: the normal threshold, or a lower floor for the human_support path (never below it)
    floor = policy["sensitive_min_confidence"] if sensitive else policy["confidence_threshold"]
    if out.confidence < floor:
        return FinalDecision("wait", "low_confidence", **base)

    # 4. back-off after feedback for the same event (checked first: "the customer said no" is the most
    #    informative reason, so it is reported before the caps)
    blocked = _within_backoff(out.life_event, history, as_of, policy)
    if blocked:
        return FinalDecision("suppress", blocked, **base)

    # 5. frequency cap: at most N proactive contacts per rolling 7 days
    recent = [d for d, _ in history.contacts if 0 <= (as_of - d).days < 7]
    if len(recent) >= policy["max_contacts_per_7_days"]:
        return FinalDecision("suppress", "frequency_cap", **base)

    # 5b. one contact per event per 30 days, even after the 7-day cap has expired
    same_event = [d for d, ev in history.contacts if ev == out.life_event and 0 <= (as_of - d).days < 30]
    if len(same_event) >= policy["max_contacts_per_event_30_days"]:
        return FinalDecision("suppress", "already_contacted_for_event", **base)

    # 2. sensitive: human support only, no automated offer (static text, nothing from the LLM)
    if sensitive:
        return FinalDecision("contact", "sensitive", channel="human_support", top_need=None,
                             message=HUMAN_SUPPORT_MESSAGE, why=HUMAN_SUPPORT_WHY, **base)

    # 6. stress: only budget_check / support, never sales needs
    catalog = config.need_catalog(out.life_event)
    top, message = out.top_action, out.message
    stressed, stress_why = stress_suspected(out, state, policy)
    notes: list[str] = [f"stress:{w}" for w in stress_why]
    reason = None
    if stressed:
        reason = "stress_restricted"
        if BUDGET_NEED in catalog:
            allowed = BUDGET_NEED
        else:   # e.g. retirement_approaching has no budget_check: keep the LLM's need unless it is a sales need
            non_sales = [n for n in catalog if n not in policy["sales_needs"]]
            allowed = top if top in non_sales else (non_sales[0] if non_sales else "none")
        if top != allowed:
            top = allowed
            title = catalog.get(allowed, {}).get("title", "Talk to us")
            message = f"{title}? It takes one tap and there is no obligation."
            notes.append("top_need_replaced_by_stress_rule")
    if top == "none" or top not in catalog:
        return FinalDecision("suppress", "no_valid_need", **base)

    # 7. channel resolution
    channel = resolve_channel(out.suggested_channel, out.urgency, state, policy)

    # 8. contact
    return FinalDecision("contact", reason, channel=channel, top_need=top, message=message, why=out.why,
                         stress=stressed, notes=notes, **base)
