"""Run the life-moment pipeline for one day, a daily replay, or the hard cases.

  run_pipeline.py --as-of 2026-09-30 [--limit N]
  run_pipeline.py --from 2026-09-05 --to 2026-09-30 [--customer C0001]
  run_pipeline.py --demo                      # D001-D007 at the demo date: expected vs actual
  run_pipeline.py --from ... --to ... --dry-run   # count the LLM calls the replay needs; no API calls
"""
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from engine.config import demo_as_of_default, load_config, load_demo  # noqa: E402
from engine.db import connect  # noqa: E402
from engine.demo_check import check_hard_cases  # noqa: E402
from engine.llm_judge import ModelNotConfigured, get_model  # noqa: E402
from engine.standin_judge import STANDIN_MODEL, standin_judge  # noqa: E402
from engine.pipeline import (RunStats, count_judge_calls, daterange, default_judge_fn, run,  # noqa: E402
                             run_range)


def print_table(conn, customer_id: str, start: date, end: date) -> None:
    print(f"\n{customer_id}: {start} -> {end}")
    print(f"{'date':11} {'flagged':8} {'conf':>5}  {'action':9} {'reason':18} {'channel':14} top_need")
    for r in conn.execute("SELECT as_of,triage_flagged,confidence,final_action,suppress_reason,channel,top_need,"
                          "event_type FROM decisions WHERE customer_id=? AND as_of BETWEEN ? AND ? "
                          "ORDER BY as_of", (customer_id, start.isoformat(), end.isoformat())):
        conf = f"{r['confidence']:.2f}" if r["confidence"] is not None else "  - "
        print(f"{r['as_of']:11} {'yes' if r['triage_flagged'] else 'no':8} {conf:>5}  "
              f"{r['final_action'] or '-':9} {r['suppress_reason'] or '-':18} {r['channel'] or '-':14} "
              f"{r['top_need'] or '-'}")


def compare_demo(conn, demo: dict, as_of: date) -> int:
    mismatches = 0
    print(f"\nHard cases up to {as_of}: expected vs actual "
          f"(expected contact = first contact decision; otherwise the decision on {as_of})")
    for r in check_hard_cases(conn, demo["expected"], as_of):
        mismatches += not r["ok"]
        conf = f"{r['confidence']:.2f}" if r.get("confidence") is not None else "-"
        print(f"{r['customer_id']}  {'OK      ' if r['ok'] else 'MISMATCH'} expected={r['expected']}\n"
              f"      actual={r['actual']} on {r.get('decision_date')} event={r.get('event_type')} conf={conf}")
    return mismatches


def print_stats(label: str, s: RunStats) -> None:
    print(f"\n== {label} ==")
    print(f"customers={s.customers} flagged(ever/summed)={s.flagged} llm_calls={s.llm_calls} "
          f"cache_hits={s.cache_hits} reused={s.reused} tokens_in={s.input_tokens} tokens_out={s.output_tokens}")
    print(f"actions={s.by_action}\nreasons={s.by_reason}\ncontacts_by_event={s.contacts_by_event}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--as-of")
    ap.add_argument("--from", dest="start")
    ap.add_argument("--to", dest="end")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--customer", action="append", help="customer id (repeatable)")
    ap.add_argument("--demo", action="store_true", help="run D001-D007 and compare with expected outcomes")
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--standin", action="store_true",
                    help="use the deterministic STAND-IN judge (not an LLM) when no model is configured; "
                         "combine with --reset to start from empty decisions")
    ap.add_argument("--reset", action="store_true", help="delete decisions, contacts and llm_log before running")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--workers", type=int, default=8)
    a = ap.parse_args()

    config, demo, conn = load_config(), load_demo(), connect()
    ids = a.customer or (list(demo["demo_customers"]) if a.demo else None)
    if ids is None and a.limit:
        ids = [r[0] for r in conn.execute("SELECT id FROM customers ORDER BY id LIMIT ?", (a.limit,))]

    start = date.fromisoformat(a.start) if a.start else None
    end = date.fromisoformat(a.end) if a.end else None
    as_of = date.fromisoformat(a.as_of) if a.as_of else (end if (a.demo and not end) else None)
    if not (start or as_of):
        as_of = demo_as_of_default()

    if a.dry_run:
        s = count_judge_calls(conn, start or as_of, end or as_of, config, ids)
        print(f"DRY RUN {start or as_of} -> {end or as_of}: customers={s.customers} flagged_ever={s.flagged} "
              f"judge calls needed (distinct fingerprints) = {s.would_call}")
        return 0

    if a.standin:
        model = STANDIN_MODEL
        print(f"*** STAND-IN JUDGE ({model}): deterministic heuristics, NOT an LLM. Results are not LLM evidence. ***")
        judge_fn = lambda ctx: standin_judge(ctx, config)  # noqa: E731
    else:
        try:
            model = get_model()
            print(f"model={model} cache={'off' if a.no_cache else 'on'}")
        except ModelNotConfigured as exc:
            print(f"ERROR: {exc}. The pipeline needs the LLM judge for flagged customers "
                  f"(or pass --standin for the non-LLM demo judge).")
            return 2
        judge_fn = default_judge_fn(config, use_cache=not a.no_cache, model=model)
    if a.reset:
        for t in ("decisions", "contacts", "llm_log"):
            conn.execute(f"DELETE FROM {t}")
        conn.commit()
        print("reset: decisions, contacts, llm_log cleared")

    if start and end:
        stats = run_range(conn, start, end, config, judge_fn, ids, workers=a.workers, judge_model=model)
        print_stats(f"replay {start} -> {end}", stats)
        for cid in (ids if ids and len(ids) <= 3 else []):
            print_table(conn, cid, start, end)
    else:
        stats = run(conn, as_of, config, judge_fn, ids, workers=a.workers, judge_model=model)
        print_stats(f"run {as_of}", stats)
        if a.demo:
            return 1 if compare_demo(conn, demo, as_of) else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
