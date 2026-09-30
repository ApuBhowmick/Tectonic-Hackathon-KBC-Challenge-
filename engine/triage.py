"""Triage: cheap, pure-code flagging (TECH_SPEC 5). Swappable behind the `Triage` protocol."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from engine.signals import Signal


@dataclass(frozen=True)
class TriageResult:
    flagged: bool
    signals: list[Signal]
    events: list[str] = field(default_factory=list)   # event types that triggered the flag
    reason: str = ""


class Triage(Protocol):
    """Stable interface so an ML model can replace the rules without touching the pipeline."""

    def flag(self, signals: list[Signal]) -> TriageResult: ...


class RuleTriage:
    """flag = any strong signal, or >= 2 distinct weak/medium signals, evaluated per event type."""

    def flag(self, signals: list[Signal]) -> TriageResult:
        by_event: dict[str, list[Signal]] = {}
        for s in signals:
            by_event.setdefault(s.event_type, []).append(s)
        events: list[str] = []
        reasons: list[str] = []
        for event_type, sigs in by_event.items():
            strong = sorted({s.name for s in sigs if s.strength == "strong"})
            soft = sorted({s.name for s in sigs if s.strength != "strong"})
            if strong:
                events.append(event_type)
                reasons.append(f"{event_type}: strong {strong}")
            elif len(soft) >= 2:
                events.append(event_type)
                reasons.append(f"{event_type}: weak/medium {soft}")
        return TriageResult(bool(events), signals, events, "; ".join(reasons))
