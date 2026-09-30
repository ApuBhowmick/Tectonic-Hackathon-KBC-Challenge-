"""Data access and business logic for the API. All SQL is parameterised; every object fetched by id is scoped
to the customer who owns it."""
from __future__ import annotations

import json
import sqlite3
from datetime import date
from typing import Any

import yaml

from api.models import (AcceptOut, DayOut, EvidenceOut, SignalOut, SuggestionOut, TimelineOut, WhyOut)
from api.settings import Settings
from engine.config import Config, load_demo
from engine.demo_check import check_hard_cases
from engine.db import REPO_ROOT
from engine.features import load_profile
from engine.schemas import JudgeOutput
from engine.signals import extract_signals
from engine.standin_judge import STANDIN_MODEL

CATEGORY_LABEL = {"transactions": "Card and account transactions", "app_events": "App behaviour",
                  "kate": "Kate chat messages", "location": "Location (city of your card payments)"}
DISMISSED = ("not_now", "not_me")


class ApiError(Exception):
    def __init__(self, status: int, detail: str):
        self.status, self.detail = status, detail


def cap_as_of(requested: date | None, settings: Settings) -> date:
    """Customers and the replay can never look past the demo clock."""
    return min(requested, settings.demo_today) if requested else settings.demo_today


def analysis_source(model: str | None) -> str:
    return "demo rules (not an AI model)" if model == STANDIN_MODEL else "AI assistant"


def need_title(config: Config, event_type: str | None, need_id: str | None) -> str | None:
    if not need_id or not event_type:
        return None
    return config.need_catalog(event_type).get(need_id, {}).get("title")


def _own_decision(conn: sqlite3.Connection, customer_id: str, decision_id: int, settings: Settings) -> sqlite3.Row:
    """A contact decision that belongs to this customer, else 404 (never reveals other customers' ids)."""
    row = conn.execute("SELECT * FROM decisions WHERE id=? AND customer_id=? AND final_action='contact' "
                       "AND as_of<=?", (decision_id, customer_id, settings.demo_today.isoformat())).fetchone()
    if row is None:
        raise ApiError(404, "Not found")
    return row


def _response_for(conn: sqlite3.Connection, decision_id: int) -> str | None:
    r = conn.execute("SELECT response FROM feedback WHERE decision_id=? ORDER BY id LIMIT 1",
                     (decision_id,)).fetchone()
    return r[0] if r else None


def get_suggestion(conn: sqlite3.Connection, customer_id: str, as_of: date, config: Config) -> SuggestionOut:
    r = conn.execute("SELECT * FROM decisions WHERE customer_id=? AND final_action='contact' AND as_of<=? "
                     "ORDER BY as_of DESC, id DESC LIMIT 1", (customer_id, as_of.isoformat())).fetchone()
    first = (load_profile(conn, customer_id).name or "").split(" ")[0] or None
    if r is None:
        return SuggestionOut(available=False, first_name=first)
    status = _response_for(conn, r["id"]) or "open"
    return SuggestionOut(available=True, first_name=first, decision_id=r["id"], as_of=r["as_of"], event_type=r["event_type"],
                         channel=r["channel"], top_need=r["top_need"],
                         top_need_title=need_title(config, r["event_type"], r["top_need"]),
                         message=r["message"], why=r["why"], status=status)


def _resolve_ref(conn: sqlite3.Connection, customer_id: str, ref: str) -> tuple[str, str, str] | None:
    """(source, when, plain text) for a ref id, only if it belongs to this customer."""
    kind = ref.split(":", 1)[0]
    if kind == "tx":
        r = conn.execute("SELECT date,merchant,amount,city FROM transactions WHERE id=? AND customer_id=?",
                         (ref, customer_id)).fetchone()
        return ("transactions", r["date"], f"Payment: {r['merchant']} (EUR {abs(r['amount']):.2f}) in "
                f"{r['city']}") if r else None
    if kind == "ev":
        r = conn.execute("SELECT timestamp,detail FROM app_events WHERE id=? AND customer_id=?",
                         (ref, customer_id)).fetchone()
        return ("app_events", r["timestamp"], f"In the app you {r['detail']}") if r else None
    if kind == "kate":
        r = conn.execute("SELECT timestamp,text FROM kate_messages WHERE id=? AND customer_id=?",
                         (ref, customer_id)).fetchone()
        return ("kate", r["timestamp"], f"You wrote to Kate: \"{r['text']}\"") if r else None
    return None


def get_why(conn: sqlite3.Connection, customer_id: str, decision_id: int, settings: Settings,
            config: Config) -> WhyOut:
    d = _own_decision(conn, customer_id, decision_id, settings)
    evidence: list[EvidenceOut] = []
    if d["llm_output"]:
        out = JudgeOutput.model_validate_json(d["llm_output"])
        for e in out.evidence:
            res = _resolve_ref(conn, customer_id, e.ref_id)
            if res:
                evidence.append(EvidenceOut(ref_id=e.ref_id, source=res[0], when=res[1], text=res[2],
                                            note=e.note))
    sigs = extract_signals(conn, customer_id, date.fromisoformat(d["as_of"]), config)
    sources = {e.source for e in evidence} | {s.source for s in sigs}
    cats = [CATEGORY_LABEL[c] for c in ("transactions", "app_events", "kate") if c in sources]
    if any(s.name == "city_shift" for s in sigs):
        cats.append(CATEGORY_LABEL["location"])
    return WhyOut(decision_id=d["id"], as_of=d["as_of"], event_type=d["event_type"], confidence=d["confidence"],
                  why=d["why"], evidence=evidence, data_categories_used=cats,
                  signals=[s.name.replace("_", " ") for s in sigs], analysis_source=analysis_source(d["judge_model"]),
                  privacy_note="You can switch each data category off in your privacy settings.")


def record_response(conn: sqlite3.Connection, customer_id: str, decision_id: int, response: str,
                    settings: Settings) -> sqlite3.Row:
    """One response per decision. Business rules: no second response, no accepting a dismissed decision."""
    d = _own_decision(conn, customer_id, decision_id, settings)
    previous = _response_for(conn, decision_id)
    if previous is not None:
        raise ApiError(409, "This suggestion has already been answered")
    conn.execute("INSERT INTO feedback(customer_id,decision_id,event_type,response,created_at) VALUES(?,?,?,?,?)",
                 (customer_id, decision_id, d["event_type"], response, f"{settings.demo_today.isoformat()}T12:00:00"))
    conn.commit()
    return d


def accept(conn: sqlite3.Connection, customer_id: str, decision_id: int, settings: Settings,
           config: Config) -> AcceptOut:
    d = record_response(conn, customer_id, decision_id, "accepted", settings)
    items: list[str] = []
    if d["top_need"]:
        needs: list[str] = [d["top_need"]]
        if d["suppress_reason"] != "stress_restricted" and d["llm_output"]:
            out = JudgeOutput.model_validate_json(d["llm_output"])
            sales = {n for n, c in config.need_catalog(d["event_type"]).items() if c.get("sales")}
            needs += [n.need_id for n in out.needs if n.need_id != d["top_need"] and n.need_id not in sales]
        items = [f"{t} (simulated)" for t in (need_title(config, d["event_type"], n) for n in needs) if t]
    else:
        items = ["A member of our team will get in touch with you (simulated)"]
    return AcceptOut(decision_id=decision_id, status="accepted", simulated=True, title="All set",
                     items=items, note="This is a demo: nothing was changed in any real system.")


def get_timeline(conn: sqlite3.Connection, customer_id: str, as_of: date, config: Config) -> TimelineOut:
    profile = load_profile(conn, customer_id)
    sigs = extract_signals(conn, customer_id, as_of, config, lookback_days=90, profile=profile)
    days = [DayOut(as_of=r["as_of"], flagged=bool(r["triage_flagged"]), confidence=r["confidence"],
                   action=r["final_action"], reason=r["suppress_reason"], event_type=r["event_type"],
                   top_need=r["top_need"], channel=r["channel"])
            for r in conn.execute("SELECT * FROM decisions WHERE customer_id=? AND as_of<=? ORDER BY as_of",
                                  (customer_id, as_of.isoformat()))]
    model = conn.execute("SELECT judge_model FROM decisions WHERE customer_id=? AND judge_model IS NOT NULL "
                         "ORDER BY as_of DESC LIMIT 1", (customer_id,)).fetchone()
    return TimelineOut(customer_id=customer_id, as_of=as_of.isoformat(), home_city=profile.city,
                       signals=[SignalOut(name=s.name, strength=s.strength, source=s.source,
                                          occurred_at=s.occurred_at, text=s.human_text) for s in sigs],
                       days=days, analysis_source=analysis_source(model[0] if model else None))


# ---------------------------------------------------------------- admin dashboard

def _pricing() -> dict[str, Any]:
    return yaml.safe_load((REPO_ROOT / "config" / "pricing.yaml").read_text()) or {}


def get_dashboard(conn: sqlite3.Connection, settings: Settings, config: Config) -> dict[str, Any]:
    q = lambda sql, *a: conn.execute(sql, a).fetchall()  # noqa: E731
    today = settings.demo_today.isoformat()
    threshold = config.policy["confidence_threshold"]
    customers = q("SELECT COUNT(*) FROM customers")[0][0]
    distinct = lambda where: q(f"SELECT COUNT(DISTINCT customer_id) FROM decisions WHERE as_of<=? AND {where}",  # noqa: E731
                               today, *([threshold] if "?" in where else []))[0][0]
    funnel = {
        "customers": customers,
        "flagged": distinct("triage_flagged=1"),
        "judged": distinct("llm_output IS NOT NULL"),
        "confident": distinct("event_type IS NOT NULL AND event_type!='none' AND confidence>=?"),
        "contacted": q("SELECT COUNT(DISTINCT customer_id) FROM contacts WHERE sent_at<=?", today + "T23:59")[0][0],
    }

    # Per-customer outcome (each flagged customer counted once): contacted, else the latest reason.
    contacted = {r[0] for r in q("SELECT DISTINCT customer_id FROM contacts")}
    latest = {r["customer_id"]: r for r in q(
        "SELECT d.* FROM decisions d JOIN (SELECT customer_id, MAX(as_of) m FROM decisions WHERE as_of<=? "
        "AND triage_flagged=1 GROUP BY customer_id) x ON x.customer_id=d.customer_id AND d.as_of=x.m", today)}
    outcomes: dict[str, int] = {}
    for cid, r in latest.items():
        key = "contacted" if cid in contacted else (r["suppress_reason"] or r["final_action"] or "unknown")
        outcomes[key] = outcomes.get(key, 0) + 1
    ever_reason = {r[0]: r[1] for r in q(
        "SELECT suppress_reason, COUNT(DISTINCT customer_id) FROM decisions WHERE triage_flagged=1 AND as_of<=? "
        "AND suppress_reason IS NOT NULL GROUP BY 1 ORDER BY 2 DESC", today)}
    contact_flags = {r[0]: r[1] for r in q(
        "SELECT suppress_reason, COUNT(DISTINCT customer_id) FROM decisions WHERE final_action='contact' "
        "AND suppress_reason IS NOT NULL AND as_of<=? GROUP BY 1", today)}
    events = {r[0]: r[1] for r in q(
        "SELECT event_type, COUNT(DISTINCT customer_id) FROM decisions WHERE final_action='contact' AND as_of<=? "
        "GROUP BY 1 ORDER BY 2 DESC", today)}

    # LLM usage and cost
    usage = []
    pricing = _pricing()
    total_cost, priced = 0.0, False
    for r in q("SELECT model, SUM(CASE WHEN cached=0 THEN 1 ELSE 0 END) calls, SUM(cached) cached, "
               "SUM(CASE WHEN cached=0 THEN input_tokens ELSE 0 END) tin, "
               "SUM(CASE WHEN cached=0 THEN output_tokens ELSE 0 END) tout FROM llm_log GROUP BY model"):
        price = (pricing.get("models") or {}).get(r["model"])
        cost = None
        if price:
            cost = r["tin"] / 1e6 * price["input_per_1m_usd"] + r["tout"] / 1e6 * price["output_per_1m_usd"]
            total_cost, priced = total_cost + cost, True
        usage.append({"model": r["model"], "is_standin": r["model"] == STANDIN_MODEL, "calls": r["calls"],
                      "cache_hits": r["cached"], "input_tokens": r["tin"], "output_tokens": r["tout"],
                      "estimated_cost_usd": None if cost is None else round(cost, 4)})

    days = q("SELECT COUNT(DISTINCT as_of) FROM decisions")[0][0] or 1
    real = [u for u in usage if not u["is_standin"]]
    calls_total = sum(u["calls"] for u in usage)
    tokens_in = pricing.get("assumed_input_tokens_per_call", 2600)
    tokens_out = pricing.get("assumed_output_tokens_per_call", 350)
    if real and sum(u["calls"] for u in real):
        n = sum(u["calls"] for u in real)
        tokens_in, tokens_out = sum(u["input_tokens"] for u in real) / n, sum(u["output_tokens"] for u in real) / n
    price_cfg = next(iter((pricing.get("models") or {}).values()), None)
    cost_per_call = (tokens_in / 1e6 * price_cfg["input_per_1m_usd"] + tokens_out / 1e6 * price_cfg["output_per_1m_usd"]
                     ) if price_cfg else None
    pop = pricing.get("projection", {}).get("customers", 2_300_000)
    flag_rate = funnel["flagged"] / customers
    daily_calls_reuse = calls_total / days / customers
    projection = {
        "customers": pop,
        "daily_llm_calls": {"no_triage": pop, "with_triage": round(pop * flag_rate),
                            "with_triage_and_fingerprint_reuse": round(pop * daily_calls_reuse)},
        "flag_rate_ever": round(flag_rate, 4), "avg_calls_per_customer_per_day": round(daily_calls_reuse, 5),
        "daily_cost_usd": None if cost_per_call is None else {
            "no_triage": round(pop * cost_per_call, 2), "with_triage": round(pop * flag_rate * cost_per_call, 2),
            "with_triage_and_fingerprint_reuse": round(pop * daily_calls_reuse * cost_per_call, 2)},
        "assumptions": {"tokens_in_per_call": round(tokens_in), "tokens_out_per_call": round(tokens_out),
                        "tokens_source": "measured from llm_log" if real else "estimate (no real LLM calls logged)",
                        "price_per_1m_usd": price_cfg or "not configured: set config/pricing.yaml",
                        "note": "Synthetic population: 12% of customers have a planted event, so the flag rate is "
                                "much higher than a real population's would be."}}

    eval_path = REPO_ROOT / "data" / "eval_report.json"
    accuracy = json.loads(eval_path.read_text()) if eval_path.exists() else None

    return {
        "as_of": today, "funnel": funnel, "outcome_by_customer": outcomes,
        "suppression_reasons_ever_hit": ever_reason, "contact_modifiers": contact_flags,
        "events_contacted": events, "llm": {"usage": usage, "total_estimated_cost_usd": round(total_cost, 4) if priced else None,
                                            "standin_in_use": any(u["is_standin"] for u in usage)},
        "projection": projection, "accuracy": accuracy, "hard_cases": hard_cases(conn, settings),
        "label": "synthetic data: the generator plants clean signals, so near-perfect scores are expected",
    }


def hard_cases(conn: sqlite3.Connection, settings: Settings) -> list[dict[str, Any]]:
    return check_hard_cases(conn, load_demo().get("expected") or {}, settings.demo_today)
