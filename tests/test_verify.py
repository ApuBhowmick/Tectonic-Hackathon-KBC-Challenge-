from datetime import date

import pytest

from engine.config import load_config
from engine.context import build_context
from engine.verify import need_relevant, verify
from tests.helpers import make_output

CFG = load_config()


@pytest.fixture()
def olivier(conn):
    return build_context(conn, "C0001", date(2026, 9, 14), CFG)


def test_hallucinated_ref_id_is_dropped(olivier):
    out = make_output(evidence=[{"ref_id": "tx:32", "note": "real"}, {"ref_id": "tx:999999", "note": "made up"}])
    v = verify(out, olivier, CFG)
    assert [e.ref_id for e in v.output.evidence] == ["tx:32"]
    assert "dropped_ref:tx:999999" in v.notes


def test_other_customers_ref_is_dropped(olivier):
    # tx:1 exists in the database but not in Olivier's context
    v = verify(make_output(evidence=[{"ref_id": "tx:1", "note": "someone else"}]), olivier, CFG)
    assert v.output.evidence == []


def test_no_verified_evidence_caps_confidence(olivier):
    v = verify(make_output(confidence=0.95, evidence=[{"ref_id": "tx:999999", "note": "x"}]), olivier, CFG)
    assert v.output.confidence == 0.3 and "evidence_unverified" in v.notes


def test_none_event_does_not_need_evidence(olivier):
    v = verify(make_output(life_event="none", stage="none", confidence=0.1, evidence=[], needs=[],
                           top_action="none", suggested_channel="none", rejection_reason="decoy"), olivier, CFG)
    assert "evidence_unverified" not in v.notes and v.output.confidence == 0.1


def test_unknown_need_and_top_action_are_discarded(olivier):
    out = make_output(needs=[{"need_id": "personal_loan", "priority": 1, "reason": "x"},
                             {"need_id": "address_update", "priority": 2, "reason": "y"}],
                      top_action="personal_loan")
    v = verify(out, olivier, CFG)
    assert [n.need_id for n in v.output.needs] == ["address_update"]
    assert v.output.top_action == "address_update"
    assert "dropped_need:personal_loan" in v.notes and "invalid_top_action:personal_loan" in v.notes


def test_need_from_another_event_is_discarded(olivier):
    v = verify(make_output(needs=[{"need_id": "child_savings", "priority": 1, "reason": "x"}],
                           top_action="child_savings"), olivier, CFG)
    assert v.output.needs == [] and v.output.top_action == "none"


def test_irrelevant_need_is_dropped(conn):
    # D001 holds no Home Insurance, so home_insurance_transfer is not relevant
    ctx = build_context(conn, "D001", date(2026, 9, 30), CFG)
    assert not need_relevant("'Home Insurance' in products_held", ctx)
    v = verify(make_output(evidence=[]), ctx, CFG)
    assert "irrelevant_need:home_insurance_transfer" in v.notes
    assert [n.need_id for n in v.output.needs] == ["address_update"]


def test_relevance_expressions(olivier, conn):
    assert need_relevant("always", olivier)
    assert need_relevant("'Home Insurance' in products_held", olivier)
    assert need_relevant("no new_energy_contract signal yet", olivier)            # not yet at 09-14
    assert need_relevant("net_cashflow_30d < 0 or income_band == low", olivier)    # -932.60 at 09-14
    late = build_context(conn, "C0001", date(2026, 9, 30), CFG)
    assert not need_relevant("no new_energy_contract signal yet", late)


def test_clamp_and_truncate(olivier):
    v = verify(make_output(confidence=1.7, message="x" * 400), olivier, CFG)
    assert v.output.confidence == 1.0 and len(v.output.message) <= 280
    assert verify(make_output(confidence=-0.2), olivier, CFG).output.confidence == 0.0


def test_why_format_noted(olivier):
    v = verify(make_output(why="Because."), olivier, CFG)
    assert "why_format" in v.notes


def test_top_action_fallback_never_picks_a_sales_need_and_is_noted(olivier):
    out = make_output(top_action="personal_loan",
                      needs=[{"need_id": "savings_buffer", "priority": 1, "reason": "x"},      # sales need
                             {"need_id": "budget_check", "priority": 2, "reason": "y"}])
    v = verify(out, olivier, CFG)
    assert v.output.top_action == "budget_check" and "top_action_fallback:budget_check" in v.notes
    only_sales = verify(make_output(top_action="personal_loan",
                                    needs=[{"need_id": "savings_buffer", "priority": 1, "reason": "x"}]), olivier, CFG)
    assert only_sales.output.top_action == "none" and "top_action_fallback:none" in only_sales.notes
