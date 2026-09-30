"""Test helpers: fake OpenAI client and JudgeOutput builders (offline, deterministic)."""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from engine.schemas import JudgeOutput


def make_output(**over: Any) -> JudgeOutput:
    base: dict[str, Any] = dict(
        life_event="moving_house", stage="in_progress", confidence=0.85,
        evidence=[{"ref_id": "tx:32", "note": "rent deposit for a new home"}],
        needs=[{"need_id": "home_insurance_transfer", "priority": 1, "reason": "policy on old address"},
               {"need_id": "address_update", "priority": 2, "reason": "address changes everywhere"}],
        top_action="home_insurance_transfer", suggested_channel="kate_message", urgency="medium",
        message="Moving? We can move your home insurance and update your address in one tap.",
        why="We noticed a rent deposit and a moving company payment.",
        financial_stress_suspected=False, sensitive=False, rejection_reason=None)
    base.update(over)
    return JudgeOutput.model_validate(base)


class FakeClient:
    """Mimics client.chat.completions.parse. `script` items: a JudgeOutput, a dict (raw parsed dump),
    or an Exception instance to raise."""

    def __init__(self, *script: Any, in_tokens: int = 1200, out_tokens: int = 250):
        self.script = list(script)
        self.calls: list[dict[str, Any]] = []
        self.in_tokens, self.out_tokens = in_tokens, out_tokens
        self.chat = SimpleNamespace(completions=SimpleNamespace(parse=self._parse))

    def _parse(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        item = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if isinstance(item, Exception):
            raise item
        parsed = None if item is None else (item if not isinstance(item, dict) else
                                            SimpleNamespace(model_dump=lambda d=item: d))
        msg = SimpleNamespace(parsed=parsed, refusal="refused" if parsed is None else None)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)],
                               usage=SimpleNamespace(prompt_tokens=self.in_tokens,
                                                     completion_tokens=self.out_tokens))
