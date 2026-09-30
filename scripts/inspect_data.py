"""Profile the synthetic CSVs (read-only). Offline tooling: may read ground_truth."""
from __future__ import annotations

import csv
from collections import Counter
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "synthetic data"
ROUTINE = {"groceries", "income", "utilities", "transport", "dining", "entertainment", "shopping", "health", "housing"}


def load(name: str) -> list[dict[str, str]]:
    with open(DATA_DIR / name, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def main() -> None:
    customers = load("customers.csv")
    tx = load("transactions.csv")
    events = load("app_events.csv")
    kate = load("kate_messages.csv")
    gt = load("ground_truth.csv")

    print("== Row counts ==")
    for n, rows in [("customers", customers), ("transactions", tx), ("app_events", events),
                    ("kate_messages", kate), ("ground_truth", gt)]:
        print(f"{n:15} {len(rows):>7}  columns={list(rows[0].keys())}")

    print("\n== Date ranges ==")
    dates = sorted(r["date"] for r in tx)
    print("transactions:", dates[0], "->", dates[-1])
    print("app_events:  ", min(r["timestamp"] for r in events), "->", max(r["timestamp"] for r in events))
    print("kate:        ", min(r["timestamp"] for r in kate), "->", max(r["timestamp"] for r in kate))

    print("\n== Amount sign ==")
    amts = [float(r["amount"]) for r in tx]
    print(f"positive={sum(a > 0 for a in amts)} negative={sum(a < 0 for a in amts)} zero={sum(a == 0 for a in amts)}")
    inc = [float(r["amount"]) for r in tx if r["category"] == "income"]
    print(f"income rows: {len(inc)}  all positive: {all(a > 0 for a in inc)}")

    print("\n== Category counts ==")
    cats = Counter(r["category"] for r in tx)
    for c, n in cats.most_common():
        print(f"{c:20} {n}")

    print("\n== Top merchants per non-routine category ==")
    for c in sorted(cats):
        if c in ROUTINE:
            continue
        m = Counter(r["merchant"] for r in tx if r["category"] == c)
        print(f"[{c}] {m.most_common(10)}")

    print("\n== App events: event_type / detail ==")
    print(Counter(r["event_type"] for r in events))
    for d, n in Counter(r["detail"] for r in events).most_common():
        print(f"  {n:3} {d}")

    print("\n== Kate message samples ==")
    for r in kate[:5]:
        print(f"  {r['customer_id']} {r['timestamp']} {r['text']}")

    print("\n== ground_truth ==")
    gtc = Counter(r["planted_event_type"] for r in gt)
    print(dict(gtc))
    starts = sorted(r["start_date"] for r in gt if r["planted_event_type"] != "none")
    print("start_date range (planted):", starts[0], "->", starts[-1])
    planted = {r["customer_id"] for r in gt if r["planted_event_type"] != "none"}
    print("customers with app_event:", len({r['customer_id'] for r in events}),
          "| with kate:", len({r['customer_id'] for r in kate}),
          "| both subsets of planted:", {r['customer_id'] for r in events} <= planted
          and {r['customer_id'] for r in kate} <= planted)
    print("products_held sample:", customers[0]["products_held"])

    home = {r["id"]: r["city"] for r in customers}
    away = Counter(r["customer_id"] for r in tx if r["city"] != home[r["customer_id"]])
    print(f"tx in non-home city: {sum(away.values())} across {len(away)} customers")

    print("\n== C0001 ==")
    c = next(r for r in customers if r["id"] == "C0001")
    print(c)
    print([r for r in gt if r["customer_id"] == "C0001"])
    for r in sorted((r for r in tx if r["customer_id"] == "C0001" and r["date"] >= "2026-09-01"), key=lambda r: r["date"]):
        print(f"  {r['date']} {float(r['amount']):>9.2f} {r['merchant']:35} {r['category']:16} {r['city']}")
    for r in events:
        if r["customer_id"] == "C0001":
            print("  APP ", r["timestamp"], r["event_type"], r["detail"])
    for r in kate:
        if r["customer_id"] == "C0001":
            print("  KATE", r["timestamp"], r["text"])


if __name__ == "__main__":
    main()
