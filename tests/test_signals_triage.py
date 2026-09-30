import re
from collections import defaultdict
from datetime import date
from pathlib import Path

import pytest

from engine.config import load_config
from engine.signals import extract_signals
from engine.triage import RuleTriage

ROOT = Path(__file__).resolve().parent.parent
CFG = load_config()
TRIAGE = RuleTriage()


def names(conn, cid, as_of, **kw):
    return [s.name for s in extract_signals(conn, cid, date.fromisoformat(as_of), CFG, **kw)]


def test_olivier_signal_timeline(conn):
    assert names(conn, "C0001", "2026-09-09") == []
    assert names(conn, "C0001", "2026-09-12") == ["rent_deposit", "moving_company"]
    assert names(conn, "C0001", "2026-09-14") == ["rent_deposit", "moving_company", "address_search"]
    sigs = extract_signals(conn, "C0001", date(2026, 9, 14), CFG)
    assert all(s.strength == "strong" for s in sigs)


def test_olivier_full_picture_at_month_end(conn):
    got = set(names(conn, "C0001", "2026-09-30"))
    assert got == {"rent_deposit", "moving_company", "address_search", "kate_address_question",
                   "furniture_spend", "home_improvement_spend", "new_energy_contract", "city_shift"}


def test_rows_after_as_of_are_ignored(conn):
    # the Kate message (09-15) and the IKEA purchase (09-17) must not leak into 09-14
    sigs = extract_signals(conn, "C0001", date(2026, 9, 14), CFG)
    assert not any(s.source == "kate" for s in sigs)
    assert all(s.occurred_at[:10] <= "2026-09-14" for s in sigs)


def test_signals_carry_ref_ids_of_real_rows(conn):
    for s in extract_signals(conn, "C0001", date(2026, 9, 30), CFG):
        table = {"tx": "transactions", "ev": "app_events", "kate": "kate_messages"}[s.ref_id.split(":")[0]]
        assert conn.execute(f"SELECT 1 FROM {table} WHERE id=? AND customer_id='C0001'", (s.ref_id,)).fetchone()


def test_privacy_prefs_exclude_categories(conn):
    conn.execute("INSERT INTO privacy_prefs VALUES('C0001','kate',0)")
    conn.execute("INSERT INTO privacy_prefs VALUES('C0001','location',0)")
    try:
        got = set(names(conn, "C0001", "2026-09-30"))
        assert "kate_address_question" not in got and "city_shift" not in got
        assert "rent_deposit" in got
        conn.execute("UPDATE privacy_prefs SET enabled=0 WHERE customer_id='C0001'")
        conn.execute("INSERT OR REPLACE INTO privacy_prefs VALUES('C0001','transactions',0)")
        assert set(names(conn, "C0001", "2026-09-30")) == {"address_search"}
    finally:
        conn.execute("DELETE FROM privacy_prefs WHERE customer_id='C0001'")


def _first_planted(conn, event_type):
    return conn.execute("SELECT customer_id FROM ground_truth WHERE planted_event_type=? "
                        "AND customer_id NOT LIKE 'D%' ORDER BY customer_id", (event_type,)).fetchone()[0]


@pytest.mark.parametrize("event_type,expected", [
    ("new_baby", {"maternity_search", "kate_baby_question"}),
    ("new_job", {"employer_details_search", "new_employer_salary", "kate_new_job"}),
    ("starting_business", {"self_employed_search", "company_registration", "business_setup_transfer",
                           "kate_freelancer"}),
    ("retirement_approaching", {"pension_search", "pension_simulation_request", "pension_savings_transfer",
                                "kate_pension_question"}),
])
def test_one_planted_customer_per_other_event(conn, event_type, expected):
    cid = _first_planted(conn, event_type)
    sigs = extract_signals(conn, cid, date(2026, 9, 30), CFG)
    res = TRIAGE.flag(sigs)
    assert res.flagged and event_type in res.events
    assert expected <= {s.name for s in sigs if s.event_type == event_type}


def test_triage_population_at_month_end(conn):
    rows = conn.execute("SELECT customer_id, planted_event_type FROM ground_truth").fetchall()
    flagged = {r[0]: TRIAGE.flag(extract_signals(conn, r[0], date(2026, 9, 30), CFG)) for r in rows}
    planted = [r[0] for r in rows if r[1] != "none" and not r[0].startswith("D")]
    none_orig = [r[0] for r in rows if r[1] == "none" and not r[0].startswith("D")]
    assert len(planted) == 49 and len(none_orig) == 351
    assert all(flagged[c].flagged for c in planted)
    false_pos = [c for c in none_orig if flagged[c].flagged]
    print(f"\nnone customers flagged by triage (of 351): {len(false_pos)} {false_pos}")
    assert false_pos == []


def test_hard_cases_triage(conn):
    d1 = TRIAGE.flag(extract_signals(conn, "D001", date(2026, 9, 30), CFG))
    assert d1.flagged and {s.name for s in d1.signals} == {"furniture_spend", "home_improvement_spend"}
    assert all(s.strength == "weak" for s in d1.signals)
    d6 = TRIAGE.flag(extract_signals(conn, "D006", date(2026, 9, 30), CFG))
    assert d6.flagged and [s.name for s in d6.signals] == ["rent_deposit"]
    assert d6.signals[0].strength == "strong"


def test_single_weak_signal_is_not_flagged(conn):
    # D001 after only the first IKEA purchase: one weak signal -> not flagged
    sigs = extract_signals(conn, "D001", date(2026, 9, 24), CFG)
    assert [s.name for s in sigs] == ["furniture_spend"] and not TRIAGE.flag(sigs).flagged


def test_signal_customer_counts_against_real_data(base_conn, capsys):
    """Customers matched by each configured signal in the 400 imported customers (no lookback limit)."""
    ids = [r[0] for r in base_conn.execute("SELECT id FROM customers")]
    counts: dict[str, set[str]] = defaultdict(set)
    for cid in ids:
        for s in extract_signals(base_conn, cid, date(2026, 9, 30), CFG, lookback_days=365):
            counts[s.name].add(cid)
    table = {n: len(v) for n, v in sorted(counts.items())}
    with capsys.disabled():
        print("\nsignal -> customers matched (400 imported customers):")
        for n, k in table.items():
            print(f"  {n:28} {k}")
    assert table["new_energy_contract"] == 11
    assert table["rent_deposit"] == 11
    assert table["moving_company"] == 11
    assert table["furniture_spend"] == 11
    assert table["home_improvement_spend"] == 1
    # every configured signal matches someone
    configured = {n for b in CFG.events.values() for n in b["signals"]}
    assert configured == set(table), configured ^ set(table)


def test_engine_and_api_never_read_ground_truth():
    offenders = []
    for folder in ("engine", "api"):
        for p in (ROOT / folder).rglob("*.py"):
            if p.name == "db.py":   # only declares the table in the schema; never queries it
                continue
            if re.search(r"ground_truth", p.read_text()):
                offenders.append(str(p))
    assert offenders == []
