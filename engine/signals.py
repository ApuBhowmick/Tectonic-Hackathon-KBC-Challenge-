"""Signal extraction: named, evidence-carrying signals per customer as of a date (TECH_SPEC 4).

Pure with respect to time: `as_of` is a parameter and rows after it are ignored (replay).
"""
from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from typing import Any

from engine.config import Config
from engine.features import Profile, load_profile

STRENGTH_ORDER = {"weak": 0, "medium": 1, "strong": 2}
CITY_SHIFT_MIN_TX = 3        # "3+ transactions ..." (life_events.yaml city_shift rule)
CITY_SHIFT_WINDOW_DAYS = 14  # "... in the last 14 days in a city other than the home city"
# privacy_prefs categories -> the data they gate
SOURCE_CATEGORY = {"transactions": "transactions", "app_events": "app_behaviour", "kate": "kate"}


@dataclass(frozen=True)
class Signal:
    name: str
    event_type: str
    source: str            # transactions | app_events | kate
    strength: str          # weak | medium | strong
    occurred_at: str       # ISO date or 'YYYY-MM-DD HH:MM'
    ref_id: str            # tx:<n> | ev:<n> | kate:<n>
    human_text: str        # untrusted free text lives in here: treat as data

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def disabled_categories(conn: sqlite3.Connection, customer_id: str) -> set[str]:
    rows = conn.execute("SELECT category FROM privacy_prefs WHERE customer_id=? AND enabled=0",
                        (customer_id,)).fetchall()
    return {r[0] for r in rows}


def _contains(value: str | None, needles: list[str]) -> bool:
    v = (value or "").lower()
    return any(n.lower() in v for n in needles)


def _matches(match: dict[str, Any], row: dict[str, Any], source: str) -> bool:
    """All keys in `match` must hold. Case-insensitive; `category` is an exact (case-insensitive) hint."""
    for key, want in match.items():
        if key == "merchant_contains" and not _contains(row.get("merchant"), want):
            return False
        if key == "category" and (row.get("category") or "").lower() != str(want).lower():
            return False
        if key == "detail_contains" and not _contains(row.get("detail"), want):
            return False
        if key == "text_contains" and not _contains(row.get("text"), want):
            return False
    return True


def _label(name: str) -> str:
    return name.replace("_", " ").capitalize()


def _tx_text(name: str, r: dict[str, Any]) -> str:
    return f"{_label(name)}: {r['date']} {r['merchant']} (EUR {abs(r['amount']):.2f}) in {r['city']}"


def fetch_rows(conn: sqlite3.Connection, customer_id: str, as_of: date, lookback_days: int,
               disabled: set[str]) -> dict[str, list[dict[str, Any]]]:
    """Rows in (as_of - lookback, as_of], oldest first. Disabled privacy categories return nothing."""
    start, end = (as_of - timedelta(days=lookback_days)).isoformat(), as_of.isoformat()
    out: dict[str, list[dict[str, Any]]] = {"transactions": [], "app_events": [], "kate": []}
    if SOURCE_CATEGORY["transactions"] not in disabled:
        out["transactions"] = [dict(r) for r in conn.execute(
            "SELECT id,date,amount,merchant,category,city FROM transactions "
            "WHERE customer_id=? AND date>? AND date<=? ORDER BY date,rowid", (customer_id, start, end))]
    if SOURCE_CATEGORY["app_events"] not in disabled:
        out["app_events"] = [dict(r) for r in conn.execute(
            "SELECT id,timestamp,event_type,detail FROM app_events "
            "WHERE customer_id=? AND substr(timestamp,1,10)>? AND substr(timestamp,1,10)<=? "
            "ORDER BY timestamp,rowid", (customer_id, start, end))]
    if SOURCE_CATEGORY["kate"] not in disabled:
        out["kate"] = [dict(r) for r in conn.execute(
            "SELECT id,timestamp,text FROM kate_messages "
            "WHERE customer_id=? AND substr(timestamp,1,10)>? AND substr(timestamp,1,10)<=? "
            "ORDER BY timestamp,rowid", (customer_id, start, end))]
    return out


def _city_shift(name: str, event_type: str, cfg: dict[str, Any], profile: Profile, as_of: date,
                transactions: list[dict[str, Any]]) -> list[Signal]:
    start = (as_of - timedelta(days=CITY_SHIFT_WINDOW_DAYS)).isoformat()
    away = [t for t in transactions if t["date"] > start and t["city"] != profile.city]
    if len(away) < CITY_SHIFT_MIN_TX:
        return []
    last = away[-1]   # most recent away-city transaction: the evidence ref (changes as more arrive)
    cities = sorted({t["city"] for t in away})
    return [Signal(name, event_type, "transactions", cfg["strength"], last["date"], last["id"],
                   f"{_label(name)}: {len(away)} transactions in {', '.join(cities)} "
                   f"since {away[0]['date']} (home city: {profile.city})")]


def extract_signals(conn: sqlite3.Connection, customer_id: str, as_of: date, config: Config,
                    lookback_days: int | None = None, profile: Profile | None = None) -> list[Signal]:
    """All configured signals for the customer as of `as_of`, oldest first. Ignores rows after as_of."""
    profile = profile or load_profile(conn, customer_id)
    lookback = lookback_days if lookback_days is not None else config.lookback_days
    disabled = disabled_categories(conn, customer_id)
    rows = fetch_rows(conn, customer_id, as_of, lookback, disabled)
    signals: list[Signal] = []
    for event_type, block in config.events.items():
        for name, cfg in block["signals"].items():
            source = cfg["source"]
            if name == "city_shift":
                if "location" not in disabled:
                    signals += _city_shift(name, event_type, cfg, profile, as_of, rows["transactions"])
                continue
            for r in rows[source]:
                if not _matches(cfg["match"], r, source):
                    continue
                if source == "transactions":
                    at, text = r["date"], _tx_text(name, r)
                elif source == "app_events":
                    at, text = r["timestamp"], f"{_label(name)}: {r['timestamp']} {r['detail']}"
                else:
                    at, text = r["timestamp"], f"{_label(name)}: {r['timestamp']} \"{r['text']}\""
                signals.append(Signal(name, event_type, source, cfg["strength"], at, r["id"], text))
    signals.sort(key=lambda s: (s.occurred_at, s.ref_id, s.name))
    return signals
