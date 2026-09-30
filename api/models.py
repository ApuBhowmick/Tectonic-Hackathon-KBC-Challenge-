"""Strict Pydantic models. Every request model forbids extra fields (no mass assignment)."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

CUSTOMER_ID_PATTERN = r"^(C\d{4}|D\d{3})$"   # C0001..C0400 and D001..D007


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LoginIn(Strict):
    customer_id: str = Field(pattern=CUSTOMER_ID_PATTERN)
    pin: str = Field(min_length=1, max_length=32)


class AdminLoginIn(Strict):
    admin_secret: str = Field(min_length=1, max_length=256)


class TokenOut(Strict):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int
    role: Literal["customer", "admin"]


class FeedbackIn(Strict):
    decision_id: int = Field(ge=1)
    response: Literal["accepted", "not_now", "not_me"]


class SuggestionOut(Strict):
    available: bool
    first_name: str | None = None
    decision_id: int | None = None
    as_of: str | None = None
    event_type: str | None = None
    channel: str | None = None
    top_need: str | None = None
    top_need_title: str | None = None
    message: str | None = None
    why: str | None = None
    status: Literal["open", "accepted", "not_now", "not_me"] | None = None


class EvidenceOut(Strict):
    ref_id: str
    source: Literal["transactions", "app_events", "kate"]
    when: str
    text: str
    note: str


class WhyOut(Strict):
    decision_id: int
    as_of: str
    event_type: str | None
    confidence: float | None
    why: str | None
    evidence: list[EvidenceOut]
    data_categories_used: list[str]
    signals: list[str]
    analysis_source: str
    privacy_note: str


class AcceptOut(Strict):
    decision_id: int
    status: Literal["accepted"]
    simulated: bool
    title: str
    items: list[str]
    note: str


class SignalOut(Strict):
    name: str
    strength: str
    source: str
    occurred_at: str
    text: str


class DayOut(Strict):
    as_of: str
    flagged: bool
    confidence: float | None
    action: str | None
    reason: str | None
    event_type: str | None
    top_need: str | None
    channel: str | None


class TimelineOut(Strict):
    customer_id: str
    as_of: str
    home_city: str
    signals: list[SignalOut]
    days: list[DayOut]
    analysis_source: str
