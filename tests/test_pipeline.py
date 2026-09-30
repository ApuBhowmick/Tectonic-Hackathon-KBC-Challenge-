import re
import shutil
import sqlite3
from datetime import date
from pathlib import Path

import pytest

from engine.config import load_config
from engine.llm_judge import JudgeResult
from engine.pipeline import count_judge_calls, run, run_for_customer, run_range
from tests.helpers import make_output

ROOT = Path(__file__).resolve().parent.parent
CFG = load_config()


@pytest.fixture()
def wconn(db_file, tmp_path):
    p = tmp_path / "work.db"
    shutil.copy(db_file, p)
    c = sqlite3.connect(p)
    c.row_factory = sqlite3.Row
    yield c
    c.close()


class Scripted:
    """Stand-in judge: confidence rises with the number of strong signals (like the intended LLM behaviour)."""

    def __init__(self, **over):
        self.calls = 0
        self.over = over

    def __call__(self, ctx):
        self.calls += 1
        strong = sum(1 for s in ctx.signals if s.strength == "strong")
        conf = 0.45 if strong <= 1 else 0.6 if strong == 2 else 0.85
        evid = [{"ref_id": ctx.signals[0].ref_id, "note": "signal"}]
        fields = dict(confidence=conf, evidence=evid)
        fields.update(self.over)
        return JudgeResult(make_output(**fields), "h", "m", 1000, 200)


def rows(conn, cid, start, end):
    return conn.execute("SELECT * FROM decisions WHERE customer_id=? AND as_of BETWEEN ? AND ? ORDER BY as_of",
                        (cid, start, end)).fetchall()


def test_olivier_replay_shape(wconn):
    judge = Scripted()
    run_range(wconn, date(2026, 9, 8), date(2026, 9, 17), CFG, judge, ["C0001"])
    got = {r["as_of"]: (r["triage_flagged"], r["final_action"], r["suppress_reason"])
           for r in rows(wconn, "C0001", "2026-09-08", "2026-09-17")}
    assert got["2026-09-09"] == (0, "suppress", "not_flagged")
    assert got["2026-09-10"] == (1, "wait", "low_confidence")
    assert got["2026-09-12"] == (1, "wait", "low_confidence")
    assert got["2026-09-13"] == (1, "wait", "low_confidence")
    assert got["2026-09-14"] == (1, "contact", None)
    assert got["2026-09-15"] == (1, "suppress", "frequency_cap")
    assert got["2026-09-16"] == (1, "suppress", "frequency_cap")
    assert wconn.execute("SELECT COUNT(*) FROM contacts WHERE customer_id='C0001'").fetchone()[0] == 1


def test_olivier_is_not_stressed_and_keeps_home_insurance_need(wconn):
    """Raw 30-day cashflow is negative (-932.60 on 09-14) only because of the move itself; the adjusted cashflow
    (without rent deposit, moving company, ...) and her income band mean no stress."""
    from engine.pipeline import _state
    from engine.signals import extract_signals
    sigs = extract_signals(wconn, "C0001", date(2026, 9, 14), CFG)
    st = _state(wconn, "C0001", date(2026, 9, 14), CFG, "moving_house", sigs)
    assert st.net_cashflow_30d == -932.6 and st.adjusted_cashflow_30d > 0
    run(wconn, date(2026, 9, 14), CFG, Scripted(top_action="home_insurance_transfer"), ["C0001"])
    r = rows(wconn, "C0001", "2026-09-14", "2026-09-14")[0]
    assert (r["final_action"], r["suppress_reason"], r["top_need"]) == ("contact", None, "home_insurance_transfer")


def test_rejudge_only_when_fingerprint_changes(wconn):
    judge = Scripted()
    # signal days for C0001: 09-10 deposit, 09-12 moving, 09-14 search, 09-15 kate -> 4 distinct fingerprints
    s = run_range(wconn, date(2026, 9, 8), date(2026, 9, 16), CFG, judge, ["C0001"])
    assert judge.calls == 4 and s.llm_calls == 4 and s.reused == 3 and s.flagged == 7
    by_day = {r["as_of"]: r["llm_called"] for r in rows(wconn, "C0001", "2026-09-08", "2026-09-16")}
    assert by_day["2026-09-11"] == 0 and by_day["2026-09-12"] == 1 and by_day["2026-09-13"] == 0
    assert count_judge_calls(wconn, date(2026, 9, 8), date(2026, 9, 16), CFG, ["C0001"]).would_call == 4


def test_idempotent_per_customer_and_day(wconn):
    judge = Scripted()
    run(wconn, date(2026, 9, 14), CFG, judge, ["C0001"])
    first = rows(wconn, "C0001", "2026-09-14", "2026-09-14")[0]
    run(wconn, date(2026, 9, 14), CFG, judge, ["C0001"])
    again = rows(wconn, "C0001", "2026-09-14", "2026-09-14")
    assert len(again) == 1 and again[0]["id"] == first["id"] and again[0]["final_action"] == "contact"
    assert wconn.execute("SELECT COUNT(*) FROM contacts WHERE customer_id='C0001'").fetchone()[0] == 1


def test_feedback_changes_fingerprint_and_suppresses(wconn):
    judge = Scripted()
    run_range(wconn, date(2026, 9, 14), date(2026, 9, 15), CFG, judge, ["C0001"])
    calls = judge.calls
    wconn.execute("INSERT INTO feedback(customer_id,decision_id,event_type,response,created_at) "
                  "VALUES('C0001',NULL,'moving_house','not_me','2026-09-16T09:00:00')")
    run(wconn, date(2026, 9, 16), CFG, judge, ["C0001"])
    assert judge.calls == calls + 1          # feedback state is part of the fingerprint
    r = rows(wconn, "C0001", "2026-09-16", "2026-09-16")[0]
    assert r["suppress_reason"] in ("frequency_cap", "recent_dismissal")
    run(wconn, date(2026, 9, 22), CFG, judge, ["C0001"])    # 7-day cap expired; the 60-day dismissal still holds
    assert rows(wconn, "C0001", "2026-09-22", "2026-09-22")[0]["suppress_reason"] == "recent_dismissal"


def test_unflagged_customers_never_reach_the_llm(wconn):
    judge = Scripted()
    ids = [r[0] for r in wconn.execute("SELECT customer_id FROM ground_truth WHERE planted_event_type='none' "
                                       "AND customer_id NOT LIKE 'D%' LIMIT 40")]
    s = run(wconn, date(2026, 9, 30), CFG, judge, ids)
    assert judge.calls == 0 and s.flagged == 0
    r = wconn.execute("SELECT triage_flagged,llm_called,final_action FROM decisions WHERE customer_id=?",
                      (ids[0],)).fetchone()
    assert tuple(r) == (0, 0, "suppress")


def test_llm_failure_suppresses_and_never_crashes(wconn):
    fail = lambda ctx: JudgeResult(None, "h", "m", error="RateLimitError: x")   # noqa: E731
    run(wconn, date(2026, 9, 14), CFG, fail, ["C0001"])
    r = rows(wconn, "C0001", "2026-09-14", "2026-09-14")[0]
    assert (r["final_action"], r["suppress_reason"], r["llm_output"]) == ("suppress", "llm_unavailable", None)
    # next day retries (nothing reusable was stored)
    ok = Scripted()
    run(wconn, date(2026, 9, 15), CFG, ok, ["C0001"])
    assert ok.calls == 1


def test_hard_cases_with_scripted_judge(wconn):
    """Plumbing check for D001-D007 with a scripted judge (real LLM behaviour is reported separately).
    D004 is stressed through the LLM flag here; the adjusted-cashflow rule is tested separately."""
    def judge(ctx):
        cid = ctx.customer_id
        if cid == "D005":
            out = make_output(life_event="moving_house", sensitive=True, suggested_channel="human_support",
                              confidence=0.9, message="Sorry for your loss, buy our loan")
        elif cid == "D004":
            out = make_output(financial_stress_suspected=True, top_action="savings_buffer", confidence=0.9,
                              needs=[{"need_id": "savings_buffer", "priority": 1, "reason": "x"}])
        elif cid == "D006":
            out = make_output(confidence=0.4)
        elif cid in ("D001", "D002"):
            out = make_output(life_event="none", stage="none", confidence=0.05, evidence=[], needs=[],
                              top_action="none", suggested_channel="none", rejection_reason="decoy")
        elif cid == "D007":   # a model that was fooled by the merchant name
            out = make_output(top_action="personal_loan", confidence=0.9,
                              needs=[{"need_id": "personal_loan", "priority": 1, "reason": "x"},
                                     {"need_id": "address_update", "priority": 2, "reason": "y"}])
        else:
            out = make_output(confidence=0.9)
        if out.life_event != "none":
            out = make_output(**{**out.model_dump(), "evidence": [{"ref_id": ctx.signals[0].ref_id, "note": "s"}]})
        return JudgeResult(out, "h", "m", 1, 1)

    ids = [f"D00{i}" for i in range(1, 8)]
    run(wconn, date(2026, 9, 30), CFG, judge, ids)
    got = {r["customer_id"]: r for r in wconn.execute("SELECT * FROM decisions WHERE as_of='2026-09-30' "
                                                      "AND customer_id LIKE 'D%'")}
    assert (got["D001"]["final_action"], got["D001"]["suppress_reason"]) == ("suppress", "no_event")
    assert (got["D002"]["final_action"], got["D002"]["suppress_reason"]) == ("suppress", "no_event")
    assert (got["D003"]["final_action"], got["D003"]["suppress_reason"]) == ("suppress", "recent_dismissal")
    assert (got["D004"]["final_action"], got["D004"]["top_need"]) == ("contact", "budget_check")
    assert (got["D005"]["channel"], got["D005"]["top_need"]) == ("human_support", None)
    assert "loan" not in got["D005"]["message"].lower()
    assert (got["D006"]["final_action"], got["D006"]["suppress_reason"]) == ("wait", "low_confidence")
    assert got["D007"]["final_action"] == "contact" and got["D007"]["top_need"] == "address_update"
    assert "loan" not in (got["D007"]["message"] or "").lower()


def test_run_for_customer_and_stats(wconn):
    s = run_for_customer(wconn, "C0001", [date(2026, 9, 14), date(2026, 9, 12)], CFG, Scripted())
    assert s.customers == 1 and s.contacts_by_event == {"moving_house": 1}


def test_engine_never_reads_the_clock():
    offenders = [str(p) for p in (ROOT / "engine").rglob("*.py")
                 if re.search(r"datetime\.now|date\.today|datetime\.utcnow|time\.time\(", p.read_text())]
    assert offenders == []


def test_olivier_contacted_once_for_the_whole_month(wconn):
    run_range(wconn, date(2026, 9, 14), date(2026, 9, 30), CFG, Scripted(), ["C0001"])
    got = {r["as_of"]: (r["final_action"], r["suppress_reason"]) for r in rows(wconn, "C0001", "2026-09-14", "2026-09-30")}
    assert got["2026-09-14"] == ("contact", None)
    assert got["2026-09-15"][1] == "frequency_cap" and got["2026-09-20"][1] == "frequency_cap"
    assert all(v == ("suppress", "already_contacted_for_event") for k, v in got.items() if k >= "2026-09-21")
    assert wconn.execute("SELECT COUNT(*) FROM contacts WHERE customer_id='C0001'").fetchone()[0] == 1
