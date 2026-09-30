"""Pydantic models for LLM I/O (TECH_SPEC 6). Used as the Structured Outputs response_format."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

LifeEvent = Literal["moving_house", "new_baby", "new_job", "starting_business",
                    "retirement_approaching", "none", "sensitive_other"]


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ref_id: str            # must exist in the provided context
    note: str              # short plain-language reason


class Need(BaseModel):
    model_config = ConfigDict(extra="forbid")
    need_id: str           # must be in the needs catalog of the detected event (validated in verify.py)
    priority: int          # 1 = most urgent
    reason: str


class JudgeOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    life_event: LifeEvent
    stage: Literal["none", "possible", "in_progress", "completed"]
    confidence: float      # 0..1 (clamped in verify.py)
    evidence: list[Evidence]
    needs: list[Need]
    top_action: str        # a need_id or "none"
    suggested_channel: Literal["app_card", "kate_message", "voice", "human_support", "none"]
    urgency: Literal["low", "medium", "high"]
    message: str           # <= 280 chars, warm, plain, one clear next step, no pressure
    why: str               # customer-facing explanation starting with "We noticed"
    financial_stress_suspected: bool
    sensitive: bool
    rejection_reason: str | None   # when life_event == none (e.g. "helping a relative")
