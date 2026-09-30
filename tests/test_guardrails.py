from dataclasses import replace
from datetime import date, timedelta

import pytest

from engine.config import load_config
from engine.features import Profile
from engine.guardrails import (HUMAN_SUPPORT_MESSAGE, CustomerState, History, decide, resolve_channel,
                               stress_suspected)
from tests.helpers import make_output

CFG = load_config()
TODAY = date(2026, 9, 30)
PROFILE = Profile("X", "X", 30, "Gent", "mid", ("Current Account", "Home Insurance"), "Gent")
STATE = CustomerState(PROFILE, net_cashflow_30d=500.0)
EMPTY = History()


def d(out=None, state=STATE, history=EMPTY, config=CFG, **over):
    return decide(out or make_output(**over), state, history, config, TODAY)


def with_policy(**kw):
    return replace(CFG, policy={**CFG.policy, **kw})


def test_0_no_llm_output_suppresses_without_crashing():
    f = decide(None, STATE, EMPTY, CFG, TODAY)
    assert (f.action, f.reason) == ("suppress", "llm_unavailable")


def test_1_no_event():
    f = d(life_event="none", stage="none", confidence=0.99, evidence=[], needs=[], top_action="none",
          suggested_channel="none", rejection_reason="relative")
    assert (f.action, f.reason) == ("suppress", "no_event")


def test_2_sensitive_flag_goes_to_human_support_only():
    f = d(sensitive=True, confidence=0.6, message="Buy our loan!", suggested_channel="app_card")
    assert (f.action, f.channel, f.top_need, f.reason) == ("contact", "human_support", None, "sensitive")
    assert f.message == HUMAN_SUPPORT_MESSAGE and "loan" not in f.message.lower()   # nothing from the LLM


def test_2_sensitive_event_type_goes_to_human_support_only():
    f = d(life_event="sensitive_other", confidence=0.9, needs=[], top_action="none")
    assert (f.action, f.channel, f.top_need) == ("contact", "human_support", None)


def test_2_sensitive_still_respects_frequency_cap_and_dismissal():
    hist = History(contacts=((TODAY - timedelta(days=2), "moving_house"),))
    assert d(sensitive=True, history=hist).reason == "frequency_cap"
    fb = History(feedback=(("moving_house", "not_me", TODAY - timedelta(days=5)),))
    assert d(sensitive=True, history=fb).reason == "recent_dismissal"


def test_2_sensitive_needs_at_least_half_confidence():
    assert d(sensitive=True, confidence=0.49).reason == "low_confidence"
    assert d(sensitive=True, confidence=0.5).channel == "human_support"
    assert d(sensitive=True, confidence=0.6).channel == "human_support"      # below the 0.75 normal threshold


def test_3_low_confidence_waits_and_threshold_is_inclusive():
    assert (d(confidence=0.74).action, d(confidence=0.74).reason) == ("wait", "low_confidence")
    assert d(confidence=0.75).action == "contact"


def test_4_frequency_cap_is_a_rolling_7_days():
    assert d(history=History(contacts=((TODAY - timedelta(days=6), "moving_house"),))).reason == "frequency_cap"
    assert d(history=History(contacts=((TODAY - timedelta(days=7), "new_baby"),))).action == "contact"
    assert d(history=History(contacts=((TODAY - timedelta(days=30), "moving_house"),))).action == "contact"


def test_4b_already_contacted_for_event_within_30_days():
    old = History(contacts=((TODAY - timedelta(days=10), "moving_house"),))    # 7-day cap long expired
    f = d(history=old)
    assert (f.action, f.reason) == ("suppress", "already_contacted_for_event")
    assert d(history=History(contacts=((TODAY - timedelta(days=29), "moving_house"),))).reason == \
        "already_contacted_for_event"
    assert d(history=History(contacts=((TODAY - timedelta(days=30), "moving_house"),))).action == "contact"
    assert d(history=History(contacts=((TODAY - timedelta(days=10), "new_baby"),))).action == "contact"
    assert d(sensitive=True, history=old).reason == "already_contacted_for_event"
    # a dismissal is reported before either cap
    both = History(contacts=old.contacts, feedback=(("moving_house", "not_now", TODAY - timedelta(days=4)),))
    assert d(history=both).reason == "recent_dismissal"
    assert d(history=History(contacts=((TODAY - timedelta(days=3), "moving_house"),))).reason == "frequency_cap"
    relaxed = with_policy(max_contacts_per_event_30_days=2)
    assert d(history=old, config=relaxed).action == "contact"


@pytest.mark.parametrize("response,days,blocked,reason", [
    ("not_now", 13, True, "recent_dismissal"), ("not_now", 14, False, None),
    ("not_me", 59, True, "recent_dismissal"), ("not_me", 60, False, None),
    ("accepted", 29, True, "recently_accepted"), ("accepted", 30, False, None)])
def test_5_backoff_windows(response, days, blocked, reason):
    f = d(history=History(feedback=(("moving_house", response, TODAY - timedelta(days=days)),)))
    assert (f.action == "suppress") == blocked
    if blocked:
        assert f.reason == reason


def test_5_feedback_for_another_event_does_not_block():
    assert d(history=History(feedback=(("new_baby", "not_me", TODAY - timedelta(days=1)),))).action == "contact"


LOW = Profile("X", "X", 30, "Gent", "low", ("Current Account", "Home Insurance"), "Gent")


@pytest.mark.parametrize("state,kw", [
    (STATE, dict(financial_stress_suspected=True)),                                     # LLM flag alone
    (CustomerState(LOW, net_cashflow_30d=-900.0, adjusted_cashflow_30d=-10.0), {}),     # low income AND negative
])
def test_6_stress_restricts_to_budget_check(state, kw):
    f = d(state=state, top_action="home_insurance_transfer", **kw)
    assert f.action == "contact" and f.top_need == "budget_check" and f.reason == "stress_restricted"
    assert f.stress and "top_need_replaced_by_stress_rule" in f.notes
    assert "insurance" not in f.message.lower()                 # message rewritten for the new need
    assert f.top_need not in CFG.policy["sales_needs"]


@pytest.mark.parametrize("state", [
    CustomerState(PROFILE, net_cashflow_30d=-900.0, adjusted_cashflow_30d=-900.0),       # negative but mid income
    CustomerState(LOW, net_cashflow_30d=-900.0, adjusted_cashflow_30d=400.0),            # low income, own spend only
])
def test_6_negative_cashflow_alone_or_event_spend_alone_is_not_stress(state):
    f = d(state=state, top_action="home_insurance_transfer")
    assert not f.stress and f.top_need == "home_insurance_transfer" and f.reason is None


def test_6_stress_blocks_sales_need_chosen_by_llm():
    f = d(financial_stress_suspected=True, top_action="savings_buffer",
          needs=[{"need_id": "savings_buffer", "priority": 1, "reason": "x"}])
    assert f.top_need == "budget_check"


def test_6_stress_keeps_llm_message_if_already_budget_check():
    f = d(financial_stress_suspected=True, top_action="budget_check", message="Let's look at your budget.")
    assert f.top_need == "budget_check" and f.message == "Let's look at your budget."


def test_6_no_stress_no_restriction():
    f = d(top_action="home_insurance_transfer")
    assert f.top_need == "home_insurance_transfer" and f.reason is None and not f.stress


def test_6_stress_rules_are_policy_driven():
    st = CustomerState(LOW, net_cashflow_30d=-999.0, adjusted_cashflow_30d=-999.0)
    off = with_policy(stress_rules={"adjusted_net_cashflow_30d_below": None, "income_bands": ["low"]})
    assert not stress_suspected(make_output(), st, off.policy)[0]
    assert stress_suspected(make_output(), st, CFG.policy) == (True, ["low_income_negative_cashflow"])


def test_6_stress_in_event_without_budget_check_keeps_non_sales_need_and_blocks_sales():
    kw = dict(life_event="retirement_approaching", financial_stress_suspected=True,
              needs=[{"need_id": "pension_projection_review", "priority": 1, "reason": "x"}])
    keep = d(top_action="pension_projection_review", **kw)
    assert (keep.action, keep.top_need) == ("contact", "pension_projection_review")
    blocked = d(top_action="pension_savings_topup", **kw)       # a sales need is replaced, not offered
    assert blocked.top_need == "pension_projection_review" and blocked.top_need not in CFG.policy["sales_needs"]


def test_6_no_valid_need_suppresses():
    assert d(top_action="none", needs=[]).reason == "no_valid_need"


@pytest.mark.parametrize("suggested,urgency,voice,disabled,expected", [
    ("voice", "high", False, (), "kate_message"),       # voice disabled in policy
    ("voice", "high", True, (), "voice"),
    ("voice", "medium", True, (), "kate_message"),      # not urgent enough
    ("kate_message", "medium", False, ("kate",), "app_card"),   # no consent for Kate
    ("none", "low", False, (), "app_card"),
    ("app_card", "low", False, (), "app_card"),
])
def test_7_channel_resolution(suggested, urgency, voice, disabled, expected):
    cfg = with_policy(channels={"voice_enabled": voice, "voice_min_urgency": "high"})
    st = CustomerState(PROFILE, 0.0, disabled_categories=frozenset(disabled))
    assert resolve_channel(suggested, urgency, st, cfg.policy) == expected


def test_8_contact_happy_path():
    f = d()
    assert (f.action, f.reason, f.channel, f.top_need) == ("contact", None, "kate_message",
                                                           "home_insurance_transfer")
    assert f.message and f.why.startswith("We noticed")


def test_rule_order():
    hist = History(contacts=((TODAY - timedelta(days=1), "moving_house"),),
                   feedback=(("moving_house", "not_now", TODAY - timedelta(days=1)),))
    assert d(life_event="none", history=hist, confidence=0.1, needs=[], top_action="none").reason == "no_event"
    assert d(confidence=0.2, history=hist).reason == "low_confidence"       # 3 before 4 and 5
    assert d(history=hist).reason == "recent_dismissal"                      # dismissal before both caps
    assert d(history=History(contacts=hist.contacts)).reason == "frequency_cap"
    assert d(history=History(feedback=hist.feedback)).reason == "recent_dismissal"
