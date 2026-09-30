"""Pipeline: signals -> triage -> (cached) LLM judge -> verify -> guardrails -> decisions (TECH_SPEC 9).

Time is always a parameter. Idempotent per (customer_id, as_of). The LLM is re-consulted only when the
signal fingerprint (signal ref_ids + feedback state) changes; otherwise the previous judgement is reused.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Callable

from engine.config import Config
from engine.context import JudgeContext, build_context
from engine.features import load_profile, net_cashflow
from engine.guardrails import CustomerState, FinalDecision, History, decide
from engine.llm_judge import JudgeResult, judge, log_llm_call
from engine.schemas import JudgeOutput
from engine.signals import disabled_categories, extract_signals
from engine.triage import RuleTriage, Triage, TriageResult
from engine.verify import verify

JudgeFn = Callable[[JudgeContext], JudgeResult]


@dataclass
class RunStats:
    customers: int = 0
    flagged: int = 0
    llm_calls: int = 0          # real API calls (cache misses)
    cache_hits: int = 0
    reused: int = 0             # judgement reused because the fingerprint did not change
    would_call: int = 0         # dry run: judge calls that would be needed (cache not consulted)
    by_action: dict[str, int] = field(default_factory=dict)
    by_reason: dict[str, int] = field(default_factory=dict)
    contacts_by_event: dict[str, int] = field(default_factory=dict)
    input_tokens: int = 0
    output_tokens: int = 0

    def add(self, other: "RunStats") -> None:
        for f in ("customers", "flagged", "llm_calls", "cache_hits", "reused", "would_call",
                  "input_tokens", "output_tokens"):
            setattr(self, f, getattr(self, f) + getattr(other, f))
        for name in ("by_action", "by_reason", "contacts_by_event"):
            mine = getattr(self, name)
            for k, v in getattr(other, name).items():
                mine[k] = mine.get(k, 0) + v


@dataclass
class _Work:
    customer_id: str
    triage: TriageResult
    fingerprint: str | None = None
    ctx: JudgeContext | None = None
    reused: JudgeOutput | None = None
    reused_model: str | None = None
    result: JudgeResult | None = None


def fingerprint(conn: sqlite3.Connection, customer_id: str, as_of: date, triage: TriageResult) -> str:
    """Hash of the signal ref_ids plus the feedback state up to as_of."""
    fb = conn.execute("SELECT id,event_type,response FROM feedback WHERE customer_id=? AND "
                      "substr(created_at,1,10)<=? ORDER BY id", (customer_id, as_of.isoformat())).fetchall()
    payload = json.dumps({"signals": sorted({(s.name, s.ref_id) for s in triage.signals}),
                          "feedback": [tuple(r) for r in fb]}, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def _history(conn: sqlite3.Connection, customer_id: str, as_of: date) -> History:
    end = as_of.isoformat()
    contacts = tuple((date.fromisoformat(r[0][:10]), r[1]) for r in conn.execute(
        "SELECT c.sent_at, d.event_type FROM contacts c LEFT JOIN decisions d ON d.id=c.decision_id "
        "WHERE c.customer_id=? AND substr(c.sent_at,1,10)<? ORDER BY c.sent_at", (customer_id, end)))
    fb = tuple((r["event_type"], r["response"], date.fromisoformat(r["created_at"][:10]))
               for r in conn.execute("SELECT event_type,response,created_at FROM feedback WHERE customer_id=? "
                                     "AND substr(created_at,1,10)<=? ORDER BY created_at,id", (customer_id, end)))
    return History(contacts, fb)


def _state(conn: sqlite3.Connection, customer_id: str, as_of: date, config: Config,
           event_type: str | None = None, signals: list | None = None) -> CustomerState:
    """Customer state for guardrails. The adjusted cashflow leaves out the transactions that are the detected
    event's own signals (rent deposit, moving company, furniture ...), so the move itself is not "stress"."""
    own = {s.ref_id for s in (signals or []) if s.event_type == event_type and s.source == "transactions"
           and s.name != "city_shift"}
    net = net_cashflow(conn, customer_id, as_of)
    own_sum = 0.0
    if own:
        own_sum = conn.execute(
            f"SELECT COALESCE(SUM(amount),0) FROM transactions WHERE id IN ({','.join('?' * len(own))}) "
            "AND date>? AND date<=?", (*sorted(own), (as_of - timedelta(days=30)).isoformat(),
                                       as_of.isoformat())).fetchone()[0]
    return CustomerState(load_profile(conn, customer_id), net, round(net - own_sum, 2),
                         frozenset(disabled_categories(conn, customer_id)))


def _previous_judgement(conn: sqlite3.Connection, customer_id: str, as_of: date, fp: str,
                        judge_model: str | None) -> tuple[JudgeOutput, str | None] | None:
    """Earlier judgement with the same fingerprint. With `judge_model` set, only judgements made by that model
    are reused, so stand-in output can never stand in for a real LLM call."""
    sql = ("SELECT llm_output, judge_model FROM decisions WHERE customer_id=? AND as_of<? AND fingerprint=? "
           "AND llm_output IS NOT NULL")
    args: list = [customer_id, as_of.isoformat(), fp]
    if judge_model is not None:
        sql, args = sql + " AND judge_model=?", args + [judge_model]
    row = conn.execute(sql + " ORDER BY as_of DESC LIMIT 1", args).fetchone()
    return (JudgeOutput.model_validate_json(row[0]), row[1]) if row else None


def _persist(conn: sqlite3.Connection, w: _Work, as_of: date, final: FinalDecision, verified: JudgeOutput | None,
             notes: list[str], llm_called: bool, model: str | None) -> int:
    o = verified
    vals = (w.customer_id, as_of.isoformat(), final.event_type, o.stage if o else None, final.confidence,
            int(w.triage.flagged), int(llm_called), o.model_dump_json() if o else None, final.action,
            final.reason, final.channel, final.top_need, final.message, final.why, as_of.isoformat(),
            w.fingerprint, json.dumps(notes + final.notes), model)
    cols = ("customer_id,as_of,event_type,stage,confidence,triage_flagged,llm_called,llm_output,final_action,"
            "suppress_reason,channel,top_need,message,why,created_at,fingerprint,verify_notes,judge_model")
    existing = conn.execute("SELECT id FROM decisions WHERE customer_id=? AND as_of=?",
                            (w.customer_id, as_of.isoformat())).fetchone()
    if existing:   # idempotent: update in place (decision id stays stable), replace this run's contact
        did = existing[0]
        conn.execute("DELETE FROM contacts WHERE decision_id=?", (did,))
        conn.execute(f"UPDATE decisions SET {','.join(c + '=?' for c in cols.split(','))} WHERE id=?",
                     (*vals, did))
    else:
        cur = conn.execute(f"INSERT INTO decisions({cols}) VALUES({','.join('?' * 18)})", vals)
        did = cur.lastrowid
    if final.action == "contact":
        conn.execute("INSERT INTO contacts(customer_id,decision_id,channel,sent_at) VALUES(?,?,?,?)",
                     (w.customer_id, did, final.channel, as_of.isoformat()))
    return did


def run(conn: sqlite3.Connection, as_of: date, config: Config, judge_fn: JudgeFn,
        customer_ids: list[str] | None = None, *, triage: Triage | None = None, workers: int = 8,
        dry_run: bool = False, judge_model: str | None = None) -> RunStats:
    """Run every (or the given) customer for one day. `dry_run` counts needed judge calls, writes nothing."""
    triage = triage or RuleTriage()
    ids = customer_ids or [r[0] for r in conn.execute("SELECT id FROM customers ORDER BY id")]
    stats = RunStats(customers=len(ids))

    # phase 1 (main thread): signals, triage, fingerprint, reuse
    work: list[_Work] = []
    for cid in ids:
        res = triage.flag(extract_signals(conn, cid, as_of, config))
        w = _Work(cid, res)
        if res.flagged:
            stats.flagged += 1
            w.fingerprint = fingerprint(conn, cid, as_of, res)
            prev = _previous_judgement(conn, cid, as_of, w.fingerprint, judge_model)
            w.reused, w.reused_model = prev if prev else (None, None)
            if w.reused is None:
                w.ctx = build_context(conn, cid, as_of, config, signals=res.signals)
        work.append(w)

    to_judge = [w for w in work if w.ctx is not None]
    stats.reused = sum(1 for w in work if w.reused is not None)
    if dry_run:
        stats.would_call = len(to_judge)
        return stats

    # phase 2: LLM calls (bounded thread pool; judge() never touches the database)
    if to_judge:
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            for w, r in zip(to_judge, pool.map(lambda x: judge_fn(x.ctx), to_judge)):
                w.result = r

    # phase 3 (main thread): log, verify, guardrails, persist
    for w in work:
        verified: JudgeOutput | None = None
        notes: list[str] = []
        llm_called = False
        if w.result is not None:
            log_llm_call(conn, w.customer_id, w.result, as_of.isoformat())
            stats.input_tokens += 0 if w.result.cached else w.result.input_tokens
            stats.output_tokens += 0 if w.result.cached else w.result.output_tokens
            llm_called = True
            if w.result.cached:
                stats.cache_hits += 1
            else:
                stats.llm_calls += 1
            if w.result.output is not None:
                v = verify(w.result.output, w.ctx, config)
                verified, notes = v.output, v.notes
            else:
                notes = [f"llm_error:{w.result.error}"]
        elif w.reused is not None:
            verified, notes = w.reused, ["judgement_reused"]   # same fingerprint: same evidence, no new call

        if not w.triage.flagged:
            final = FinalDecision("suppress", "not_flagged")
        else:
            final = decide(verified, _state(conn, w.customer_id, as_of, config,
                                            verified.life_event if verified else None, w.triage.signals),
                           _history(conn, w.customer_id, as_of), config, as_of)
        model = w.result.model if w.result is not None and w.result.output is not None else w.reused_model
        _persist(conn, w, as_of, final, verified, notes, llm_called, model)
        stats.by_action[final.action] = stats.by_action.get(final.action, 0) + 1
        if final.reason and w.triage.flagged:
            stats.by_reason[final.reason] = stats.by_reason.get(final.reason, 0) + 1
        if final.action == "contact":
            stats.contacts_by_event[final.event_type or "?"] = \
                stats.contacts_by_event.get(final.event_type or "?", 0) + 1
    conn.commit()
    return stats


def daterange(start: date, end: date) -> list[date]:
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def run_range(conn: sqlite3.Connection, start: date, end: date, config: Config, judge_fn: JudgeFn,
              customer_ids: list[str] | None = None, **kw) -> RunStats:
    """Replay every day in order for the given customers (default: all)."""
    total = RunStats()
    for d in daterange(start, end):
        s = run(conn, d, config, judge_fn, customer_ids, **kw)
        total.add(s)
        total.customers = s.customers
    return total


def run_for_customer(conn: sqlite3.Connection, customer_id: str, dates: list[date], config: Config,
                     judge_fn: JudgeFn, **kw) -> RunStats:
    total = RunStats()
    for d in sorted(dates):
        total.add(run(conn, d, config, judge_fn, [customer_id], **kw))
    total.customers = 1
    return total


def count_judge_calls(conn: sqlite3.Connection, start: date, end: date, config: Config,
                      customer_ids: list[str] | None = None, triage: Triage | None = None) -> RunStats:
    """Dry run for a replay: how many judge calls the fingerprint rule needs (distinct fingerprints per
    customer), without calling the LLM or writing anything."""
    triage = triage or RuleTriage()
    ids = customer_ids or [r[0] for r in conn.execute("SELECT id FROM customers ORDER BY id")]
    seen: dict[str, set[str]] = {}
    stats = RunStats(customers=len(ids))
    flagged_ever: set[str] = set()
    for d in daterange(start, end):
        for cid in ids:
            res = triage.flag(extract_signals(conn, cid, d, config))
            if res.flagged:
                flagged_ever.add(cid)
                seen.setdefault(cid, set()).add(fingerprint(conn, cid, d, res))
    stats.flagged = len(flagged_ever)
    stats.would_call = sum(len(v) for v in seen.values())
    return stats


def default_judge_fn(config: Config, *, use_cache: bool = True, model: str | None = None) -> JudgeFn:
    return lambda ctx: judge(ctx, config, model=model, use_cache=use_cache)
