"""Record live LLM responses as test fixtures (needs OPENAI_API_KEY and OPENAI_MODEL in .env).

Runs the judge for C0001 at 2026-09-10/12/14 and D001-D007 at 2026-09-30 against data/kbc.db, caches the
responses in data/llm_cache/, copies each to tests/fixtures/llm/<id>_<as_of>.json and prints token usage
and an estimated cost (prices from config/pricing.yaml, which are assumptions).
"""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from engine.config import load_config  # noqa: E402
from engine.context import build_context  # noqa: E402
from engine.db import connect  # noqa: E402
from engine.llm_judge import ModelNotConfigured, get_model, judge, log_llm_call  # noqa: E402
from engine.verify import verify  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "llm"
TARGETS = [("C0001", "2026-09-10"), ("C0001", "2026-09-12"), ("C0001", "2026-09-14")] + \
          [(f"D00{i}", "2026-09-30") for i in range(1, 8)]


def price(model: str) -> tuple[float, float] | None:
    p = (yaml.safe_load((ROOT / "config" / "pricing.yaml").read_text()).get("models") or {}).get(model)
    return (p["input_per_1m_usd"], p["output_per_1m_usd"]) if p else None


def main() -> int:
    config = load_config()
    try:
        model = get_model()
    except ModelNotConfigured as exc:
        print(f"Cannot record fixtures: {exc}")
        return 2
    conn = connect()
    FIXTURES.mkdir(parents=True, exist_ok=True)
    tot_in = tot_out = failures = 0
    print(f"model={model}")
    print(f"{'customer':9} {'as_of':11} {'event':22} {'conf':>5} {'stage':12} {'channel':14} top_action  tokens(in/out) cached")
    for cid, as_of in TARGETS:
        ctx = build_context(conn, cid, date.fromisoformat(as_of), config)
        res = judge(ctx, config, model=model)
        if res.output is None:
            failures += 1
            print(f"{cid:9} {as_of:11} FAILED: {res.error}")
            continue
        log_llm_call(conn, cid, res, as_of)
        v = verify(res.output, ctx, config)
        o = v.output
        tot_in, tot_out = tot_in + res.input_tokens, tot_out + res.output_tokens
        print(f"{cid:9} {as_of:11} {o.life_event:22} {o.confidence:5.2f} {o.stage:12} {o.suggested_channel:14} "
              f"{o.top_action:11} {res.input_tokens}/{res.output_tokens} {res.cached}  notes={v.notes}")
        (FIXTURES / f"{cid}_{as_of}.json").write_text(json.dumps(
            {"customer_id": cid, "as_of": as_of, "model": model, "prompt_hash": res.prompt_hash,
             "usage": {"input_tokens": res.input_tokens, "output_tokens": res.output_tokens},
             "output": res.output.model_dump()}, indent=1), encoding="utf-8")
    conn.commit()
    p = price(model)
    cost = f"${tot_in / 1e6 * p[0] + tot_out / 1e6 * p[1]:.4f}" if p else "price not set (no price for this model in config/pricing.yaml)"
    print(f"\ntotal tokens: input={tot_in} output={tot_out}; estimated cost: {cost}; failures={failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
