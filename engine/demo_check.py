"""Expected-vs-actual for the hard cases D001-D007 (config/demo.yaml `expected`). Reads decisions only.

In a daily replay a customer is contacted once, on the day the evidence is strong enough; later days are
suppressed by the caps. So an expected `contact` is compared with the customer's first contact decision, and
an expected non-contact with the decision on the demo date.
"""
from __future__ import annotations

import sqlite3
from datetime import date
from typing import Any


def check_hard_cases(conn: sqlite3.Connection, expected: dict[str, dict[str, Any]], as_of: date) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for cid, exp in expected.items():
        if exp.get("action") == "contact":
            r = conn.execute("SELECT d.*, c.name FROM decisions d JOIN customers c ON c.id=d.customer_id WHERE "
                             "d.customer_id=? AND d.final_action='contact' AND d.as_of<=? ORDER BY d.as_of LIMIT 1",
                             (cid, as_of.isoformat())).fetchone()
        else:
            r = None
        if r is None:
            r = conn.execute("SELECT d.*, c.name FROM decisions d JOIN customers c ON c.id=d.customer_id "
                             "WHERE d.customer_id=? AND d.as_of=?", (cid, as_of.isoformat())).fetchone()
        if r is None:
            out.append({"customer_id": cid, "expected": exp, "actual": None, "ok": False})
            continue
        act = {"action": r["final_action"], "reason": r["suppress_reason"], "channel": r["channel"],
               "top_need": r["top_need"]}
        out.append({"customer_id": cid, "name": r["name"], "expected": exp, "actual": act,
                    "decision_date": r["as_of"], "event_type": r["event_type"], "confidence": r["confidence"],
                    "ok": all(act.get(k) == v for k, v in exp.items()), "judge_model": r["judge_model"]})
    return out
