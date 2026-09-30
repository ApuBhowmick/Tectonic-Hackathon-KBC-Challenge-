"""Precision / recall / time-to-detect vs ground_truth on the 400 original customers (offline tooling).

Reads the decisions already in the DB (run scripts/run_pipeline.py first). Detection for a planted customer =
first date with final_action == contact for the matching event. Writes data/eval_report.json.
"""
from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from engine.db import connect  # noqa: E402
from engine.standin_judge import STANDIN_MODEL  # noqa: E402

LABEL = ("synthetic data: the generator plants clean signals, so near-perfect scores are expected; "
         "the hard cases are the real test")


def main() -> dict:
    conn = connect()
    truth = {r["customer_id"]: (r["planted_event_type"], r["start_date"]) for r in conn.execute(
        "SELECT g.* FROM ground_truth g JOIN customers c ON c.id=g.customer_id WHERE c.is_demo=0")}
    first_contact: dict[str, tuple[str, str]] = {}
    for r in conn.execute("SELECT customer_id,event_type,as_of FROM decisions WHERE final_action='contact' "
                          "ORDER BY as_of,id"):
        if r["customer_id"] in truth:
            first_contact.setdefault((r["customer_id"]), (r["event_type"], r["as_of"]))
    flagged = {r[0] for r in conn.execute("SELECT DISTINCT customer_id FROM decisions WHERE triage_flagged=1")}
    models = [r[0] for r in conn.execute("SELECT DISTINCT judge_model FROM decisions WHERE judge_model IS NOT NULL")]

    events = sorted({t for t, _ in truth.values() if t != "none"})
    per_event = {}
    for ev in events:
        planted = [c for c, (t, _) in truth.items() if t == ev]
        contacted_as_ev = [c for c, (e, _) in first_contact.items() if e == ev]
        tp = [c for c in planted if first_contact.get(c, ("", ""))[0] == ev]
        days = []
        for c in tp:
            from datetime import date
            days.append((date.fromisoformat(first_contact[c][1]) - date.fromisoformat(truth[c][1])).days)
        p = len(tp) / len(contacted_as_ev) if contacted_as_ev else 0.0
        r_ = len(tp) / len(planted)
        per_event[ev] = {"planted": len(planted), "detected": len(tp), "false_positives": len(contacted_as_ev) - len(tp),
                         "precision": round(p, 3), "recall": round(r_, 3),
                         "f1": round(2 * p * r_ / (p + r_), 3) if p + r_ else 0.0,
                         "median_days_to_contact": statistics.median(days) if days else None,
                         "days_to_contact": sorted(days)}
    none_ids = [c for c, (t, _) in truth.items() if t == "none"]
    planted_ids = [c for c, (t, _) in truth.items() if t != "none"]
    report = {"label": LABEL, "customers_evaluated": len(truth), "judge_models": models,
              "standin_judge": STANDIN_MODEL in models, "per_event": per_event,
              "triage_recall": round(sum(c in flagged for c in planted_ids) / len(planted_ids), 3),
              "none_customers": len(none_ids), "none_flagged_by_triage": sum(c in flagged for c in none_ids),
              "none_contacted": sum(c in first_contact for c in none_ids)}
    (ROOT / "data").mkdir(exist_ok=True)
    (ROOT / "data" / "eval_report.json").write_text(json.dumps(report, indent=1))
    print(f"[{LABEL}]\njudge models: {models}" + ("   <-- STAND-IN, not an LLM" if report["standin_judge"] else ""))
    for ev, m in per_event.items():
        print(f"{ev:24} planted={m['planted']:2} detected={m['detected']:2} fp={m['false_positives']} "
              f"P={m['precision']:.2f} R={m['recall']:.2f} F1={m['f1']:.2f} median_days={m['median_days_to_contact']}")
    print(f"triage recall={report['triage_recall']}  none flagged={report['none_flagged_by_triage']}/"
          f"{report['none_customers']}  none contacted={report['none_contacted']}")
    return report


if __name__ == "__main__":
    main()
