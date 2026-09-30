"""Load the CSVs in `synthetic data/` into data/kbc.db (TECH_SPEC 2b).

Idempotent: the DB is rebuilt from scratch, so every run yields the same rows and ref ids.
Re-run scripts/add_demo_customers.py afterwards to add D001-D007.
"""
from __future__ import annotations

import csv
import secrets
import sys
from pathlib import Path

import bcrypt
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from engine.db import SCHEMA, connect, db_path  # noqa: E402

DATA_DIR = Path(__file__).resolve().parent.parent / "synthetic data"
CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
TABLES = ["customers", "transactions", "app_events", "kate_messages", "ground_truth"]


def read(name: str) -> list[dict[str, str]]:
    with open(DATA_DIR / name, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def name_overrides() -> dict[str, str]:
    """Display-name overrides from config/demo.yaml (the CSVs stay untouched)."""
    with open(CONFIG_DIR / "demo.yaml") as f:
        return {k: str(v) for k, v in (yaml.safe_load(f).get("name_overrides") or {}).items()}


def fixed_pins() -> dict[str, str]:
    """Documented demo PINs (config/demo.yaml); every other customer gets a random PIN."""
    with open(CONFIG_DIR / "demo.yaml") as f:
        return {k: str(v) for k, v in (yaml.safe_load(f).get("demo_pins") or {}).items()}


def new_pin() -> str:
    return f"{secrets.randbelow(10000):04d}"


def hash_pin(pin: str) -> str:
    return bcrypt.hashpw(pin.encode(), bcrypt.gensalt(rounds=10)).decode()


def main(path: Path | None = None, verbose: bool = True) -> dict[str, int]:
    target = Path(path) if path else db_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        target.unlink()
    conn = connect(target)
    conn.executescript(SCHEMA)

    pins, names = fixed_pins(), name_overrides()
    for r in read("customers.csv"):
        pin = pins.get(r["id"]) or new_pin()
        conn.execute(
            "INSERT INTO customers(id,name,age,city,income_band,products_held,address_on_file,pin_hash,is_demo)"
            " VALUES(?,?,?,?,?,?,?,?,0)",
            (r["id"], names.get(r["id"], r["name"]), int(r["age"]), r["city"], r["income_band"],
             r["products_held"], r["city"], hash_pin(pin)),
        )

    # Stable ref ids in file order: the LLM cites these, cached responses depend on them.
    conn.executemany(
        "INSERT INTO transactions(id,customer_id,date,amount,merchant,category,city) VALUES(?,?,?,?,?,?,?)",
        [(f"tx:{n}", r["customer_id"], r["date"], float(r["amount"]), r["merchant"], r["category"], r["city"])
         for n, r in enumerate(read("transactions.csv"), start=1)],
    )
    conn.executemany(
        "INSERT INTO app_events(id,customer_id,timestamp,event_type,detail) VALUES(?,?,?,?,?)",
        [(f"ev:{n}", r["customer_id"], r["timestamp"], r["event_type"], r["detail"])
         for n, r in enumerate(read("app_events.csv"), start=1)],
    )
    conn.executemany(
        "INSERT INTO kate_messages(id,customer_id,timestamp,text) VALUES(?,?,?,?)",
        [(f"kate:{n}", r["customer_id"], r["timestamp"], r["text"])
         for n, r in enumerate(read("kate_messages.csv"), start=1)],
    )
    conn.executemany(
        "INSERT INTO ground_truth(customer_id,planted_event_type,start_date) VALUES(?,?,?)",
        [(r["customer_id"], r["planted_event_type"], r["start_date"] or None) for r in read("ground_truth.csv")],
    )
    conn.commit()

    counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in TABLES}
    conn.close()

    if verbose:
        print(f"Database: {target}")
        for t, n in counts.items():
            print(f"  {t:15} {n:>7}")
        print("Fixed demo PINs are listed in the README; only bcrypt hashes are stored.")
    return counts


if __name__ == "__main__":
    main()
