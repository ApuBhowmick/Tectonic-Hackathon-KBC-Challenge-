"""Customer profile and derived cashflow features (no balance exists in the data)."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta


@dataclass(frozen=True)
class Profile:
    id: str
    name: str
    age: int
    city: str
    income_band: str
    products_held: tuple[str, ...]
    address_on_file: str


def load_profile(conn: sqlite3.Connection, customer_id: str) -> Profile:
    r = conn.execute(
        "SELECT id,name,age,city,income_band,products_held,address_on_file FROM customers WHERE id=?",
        (customer_id,)).fetchone()
    if r is None:
        raise KeyError(customer_id)
    return Profile(r["id"], r["name"], r["age"], r["city"], r["income_band"],
                   tuple(json.loads(r["products_held"])), r["address_on_file"])


def net_cashflow(conn: sqlite3.Connection, customer_id: str, as_of: date, days: int = 30) -> float:
    """Sum of all signed amounts in the `days` days up to and including as_of."""
    start = (as_of - timedelta(days=days)).isoformat()
    row = conn.execute(
        "SELECT COALESCE(SUM(amount),0) FROM transactions WHERE customer_id=? AND date>? AND date<=?",
        (customer_id, start, as_of.isoformat())).fetchone()
    return round(float(row[0]), 2)


def monthly_income_est(conn: sqlite3.Connection, customer_id: str, as_of: date) -> float:
    """Mean monthly sum of category == income, over months with at least one income row up to as_of."""
    rows = conn.execute(
        "SELECT substr(date,1,7) m, SUM(amount) FROM transactions "
        "WHERE customer_id=? AND category='income' AND date<=? GROUP BY m",
        (customer_id, as_of.isoformat())).fetchall()
    return round(sum(r[1] for r in rows) / len(rows), 2) if rows else 0.0
