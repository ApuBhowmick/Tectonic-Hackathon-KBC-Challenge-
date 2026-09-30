import shutil
import sqlite3
from pathlib import Path

import bcrypt

ROOT = Path(__file__).resolve().parent.parent
DEMO = [f"D00{i}" for i in range(1, 8)]


def test_seven_demo_customers(conn):
    rows = conn.execute("SELECT id FROM customers WHERE is_demo=1 ORDER BY id").fetchall()
    assert [r[0] for r in rows] == DEMO
    assert conn.execute("SELECT COUNT(*) FROM customers").fetchone()[0] == 407


def test_nothing_after_demo_today(conn):
    for t, col in (("transactions", "date"), ("app_events", "timestamp"), ("kate_messages", "timestamp")):
        assert conn.execute(f"SELECT MAX({col}) FROM {t}").fetchone()[0] <= "2026-09-30 23:59"


def test_ground_truth_rows(conn):
    got = dict(conn.execute("SELECT customer_id, planted_event_type FROM ground_truth WHERE customer_id LIKE 'D%'"))
    assert got == {"D001": "none", "D002": "none", "D003": "moving_house", "D004": "moving_house",
                   "D005": "sensitive_other", "D006": "moving_house", "D007": "moving_house"}


def test_d003_not_now_feedback(conn):
    r = conn.execute("SELECT event_type, response, created_at FROM feedback WHERE customer_id='D003'").fetchall()
    assert len(r) == 1 and (r[0][0], r[0][1]) == ("moving_house", "not_now") and r[0][2].startswith("2026-09-26")


def test_d004_negative_cashflow_and_low_income(conn):
    net = conn.execute("SELECT SUM(amount) FROM transactions WHERE customer_id='D004' AND date>'2026-08-31'"
                       ).fetchone()[0]
    assert net < 0
    assert conn.execute("SELECT income_band FROM customers WHERE id='D004'").fetchone()[0] == "low"


def test_d006_single_deposit_and_d007_injection(conn):
    d6 = conn.execute("SELECT date FROM transactions WHERE customer_id='D006' AND merchant LIKE 'Rent Deposit%'"
                      ).fetchall()
    assert [r[0] for r in d6] == ["2026-09-28"]
    assert conn.execute("SELECT COUNT(*) FROM transactions WHERE customer_id='D007' AND merchant LIKE "
                        "'%IGNORE PREVIOUS INSTRUCTIONS%'").fetchone()[0] == 1


def test_fixed_pins(conn):
    h = conn.execute("SELECT pin_hash FROM customers WHERE id='D004'").fetchone()[0]
    assert bcrypt.checkpw(b"4044", h.encode())


def test_idempotent_with_stable_ref_ids(db_file, tmp_path):
    import add_demo_customers as mod
    copy = tmp_path / "again.db"
    shutil.copy(db_file, copy)

    def snapshot(p):
        c = sqlite3.connect(p)
        out = (c.execute("SELECT id, customer_id, date, amount, merchant FROM transactions "
                         "WHERE customer_id LIKE 'D%' ORDER BY id").fetchall(),
               c.execute("SELECT COUNT(*) FROM transactions").fetchone()[0],
               c.execute("SELECT COUNT(*) FROM feedback").fetchone()[0])
        c.close()
        return out
    before = snapshot(copy)
    mod.main(copy, verbose=False)
    assert snapshot(copy) == before
