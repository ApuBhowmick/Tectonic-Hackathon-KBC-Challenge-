"""LLM judge: OpenAI Structured Outputs, disk cache, retry, token logging (TECH_SPEC 6).

The judge never touches the database (thread-safe for batch runs). Callers log with `log_llm_call`.
Failures never raise: the result carries `error` and `output=None` and the pipeline suppresses the
decision with reason `llm_unavailable`.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from pydantic import ValidationError

from engine.config import Config
from engine.context import JudgeContext, build_system_prompt
from engine.db import REPO_ROOT
from engine.schemas import JudgeOutput

log = logging.getLogger(__name__)

CACHE_DIR = REPO_ROOT / "data" / "llm_cache"
TIMEOUT_S = 30
MAX_RETRIES = 2
BACKOFF_S = (1.0, 3.0)


@dataclass(frozen=True)
class JudgeResult:
    output: JudgeOutput | None
    prompt_hash: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0
    cached: bool = False
    error: str | None = None       # never contains PII or the API key


class ModelNotConfigured(RuntimeError):
    pass


def get_model() -> str:
    _load_env()
    model = os.environ.get("OPENAI_MODEL", "").strip()
    if not model:
        raise ModelNotConfigured("OPENAI_MODEL is not set (put it in .env)")
    return model


def _load_env() -> None:
    try:
        from dotenv import load_dotenv
        load_dotenv(REPO_ROOT / ".env", override=False)
    except ImportError:  # pragma: no cover
        pass


def prompt_hash(model: str, system_prompt: str, user_message: str) -> str:
    return hashlib.sha256("\x1f".join([model, system_prompt, user_message]).encode("utf-8")).hexdigest()


def make_client() -> Any:
    """Real OpenAI client. Our own retry loop handles backoff, so the SDK's is disabled."""
    _load_env()
    from openai import OpenAI
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key:
        raise RuntimeError("OPENAI_API_KEY is not set (put it in .env)")
    return OpenAI(api_key=key, timeout=TIMEOUT_S, max_retries=0)


def _read_cache(path: Path) -> tuple[JudgeOutput, dict[str, Any]] | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return JudgeOutput.model_validate(raw["output"]), raw.get("usage", {})
    except (OSError, ValueError, KeyError, ValidationError):
        return None   # unreadable or invalid cache entries are treated as a miss


def _write_cache(path: Path, h: str, model: str, out: JudgeOutput, usage: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"prompt_hash": h, "model": model, "usage": usage,
                               "output": out.model_dump()}, indent=1), encoding="utf-8")
    tmp.replace(path)


def _retryable(exc: Exception) -> bool:
    import openai
    return isinstance(exc, (openai.RateLimitError, openai.APITimeoutError, openai.APIConnectionError,
                            openai.InternalServerError))


def _call(client: Any, model: str, system: str, user: str, temperature: float | None) -> Any:
    kwargs: dict[str, Any] = dict(
        model=model, response_format=JudgeOutput, timeout=TIMEOUT_S,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}])
    if temperature is not None:
        kwargs["temperature"] = temperature
    return client.chat.completions.parse(**kwargs)


def judge(ctx: JudgeContext, config: Config, *, model: str | None = None, client: Any = None,
          cache_dir: Path | None = None, use_cache: bool = True, temperature: float | None = 0.0,
          sleep: Callable[[float], None] = time.sleep) -> JudgeResult:
    model = model or get_model()
    system, user = build_system_prompt(config), ctx.user_message()
    h = prompt_hash(model, system, user)
    path = (cache_dir or CACHE_DIR) / f"{h}.json"

    if use_cache and (hit := _read_cache(path)) is not None:
        out, usage = hit
        return JudgeResult(out, h, model, usage.get("input_tokens", 0), usage.get("output_tokens", 0),
                           0, True)

    try:
        client = client or make_client()
    except Exception as exc:   # missing key etc.
        return JudgeResult(None, h, model, error=f"client_unavailable: {exc}")

    last_error = "unknown"
    for attempt in range(MAX_RETRIES + 1):
        t0 = time.monotonic()
        try:
            try:
                resp = _call(client, model, system, user, temperature)
            except Exception as exc:
                # some models reject a non-default temperature: retry once without it
                if temperature is not None and "temperature" in str(exc).lower() \
                        and not _retryable(exc):
                    temperature = None
                    resp = _call(client, model, system, user, None)
                else:
                    raise
            latency = int((time.monotonic() - t0) * 1000)
            msg = resp.choices[0].message
            if msg.parsed is None:
                return JudgeResult(None, h, model, latency_ms=latency,
                                   error=f"no_parsed_output: {getattr(msg, 'refusal', None) or 'invalid'}")
            out = JudgeOutput.model_validate(msg.parsed.model_dump())   # re-validate; reject, never patch
            usage = {"input_tokens": getattr(resp.usage, "prompt_tokens", 0) or 0,
                     "output_tokens": getattr(resp.usage, "completion_tokens", 0) or 0}
            if use_cache:
                _write_cache(path, h, model, out, usage)
            return JudgeResult(out, h, model, usage["input_tokens"], usage["output_tokens"], latency)
        except ValidationError as exc:
            return JudgeResult(None, h, model, error=f"invalid_output: {type(exc).__name__}")
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {str(exc)[:200]}"
            if _retryable(exc) and attempt < MAX_RETRIES:
                sleep(BACKOFF_S[min(attempt, len(BACKOFF_S) - 1)])
                continue
            log.warning("LLM call failed for %s: %s", ctx.customer_id, last_error)
            break
    return JudgeResult(None, h, model, error=last_error)


def log_llm_call(conn: sqlite3.Connection, customer_id: str, result: JudgeResult, created_at: str) -> None:
    """Token logging into llm_log (created_at is passed in: the engine never reads the clock)."""
    conn.execute(
        "INSERT INTO llm_log(customer_id,prompt_hash,model,input_tokens,output_tokens,latency_ms,cached,"
        "created_at) VALUES(?,?,?,?,?,?,?,?)",
        (customer_id, result.prompt_hash, result.model, result.input_tokens, result.output_tokens,
         result.latency_ms, int(result.cached), created_at))
