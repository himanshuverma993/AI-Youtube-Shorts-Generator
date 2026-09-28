"""LLM + transcription clients — three-tier, doomsday-proof.

BACKENDS
* transcription     — Groq ``whisper-large-v3-turbo`` (audio.transcriptions)
* LLM tier 1        — Groq ``llama-3.3-70b-versatile`` (primary, free tier)
* LLM tier 2        — Cerebras ``llama-3.3-70b`` (free fallback, 1M tok/day)
* LLM tier 3        — LOCAL llama.cpp model on this machine's CPU
                      (shorts_generator/local/llm.py — zero keys, zero money;
                      armed by default, disable with ``LOCAL_LLM=false``)

FAILOVER CONTRACT (the public entry point is :func:`call_llm`):
  Tiers are tried in order; on a HARD failure the IDENTICAL prompt is replayed
  on the next tier. Each cloud backend has its own circuit breaker
  (``LLM_CIRCUIT_BREAK_SECONDS``): once tripped, later calls in the run skip
  that backend (and its retry-sleep tax) and go straight to the surviving
  tier; the tripped backend is re-probed after the cooldown. With only one
  configured tier and no fallbacks behind it, behavior is exactly the classic
  single-backend path (no short-circuit), so nothing changes for simple setups.
  A fully key-less machine routes: Groq-key error (instant) → Cerebras absent
  → local CPU model — the pipeline NEVER stops for lack of an API account.

Audio transcription stays Groq-primary with its own local faster-whisper
fallback (transcriber.py) — Cerebras has no free Whisper equivalent.
"""
import importlib
import random
import time
from functools import lru_cache
from typing import Optional, Tuple

from .config import (
    CEREBRAS_LLM_MODEL,
    GROQ_LLM_MODEL,
    GROQ_MAX_RETRIES,
    GROQ_TIMEOUT_SECONDS,
    GROQ_WHISPER_MODEL,
    LLM_CIRCUIT_BREAK_SECONDS,
    cerebras_fallback_available,
    local_llm_enabled,
    require_cerebras_key,
    require_groq_key,
)
from .local.llm import call_local_llm

_JSON_SYSTEM_MESSAGE = (
    "You are a JSON API. Always reply with a single valid JSON object and nothing else."
)


# ---------------------------------------------------------------------------
# Clients (lazy, process-wide)
# ---------------------------------------------------------------------------
@lru_cache(maxsize=1)
def get_client():
    """Process-wide Groq client (constructed lazily so imports stay free)."""
    try:
        from groq import Groq  # type: ignore
    except ImportError as e:
        raise RuntimeError(
            "The groq package is required. Install it with:\n"
            "    pip install -r requirements.txt"
        ) from e
    return Groq(
        api_key=require_groq_key(),
        timeout=GROQ_TIMEOUT_SECONDS,
        max_retries=0,  # we handle retries ourselves so we control the backoff
    )


@lru_cache(maxsize=1)
def get_cerebras_client():
    """Process-wide Cerebras client, built only when the fallback is needed."""
    try:
        from cerebras.cloud.sdk import Cerebras  # type: ignore
    except ImportError as e:
        raise RuntimeError(
            "cerebras-cloud-sdk is required for the LLM fallback. Install with:\n"
            "    pip install -r requirements.txt"
        ) from e
    return Cerebras(
        api_key=require_cerebras_key(),
        timeout=GROQ_TIMEOUT_SECONDS,
        max_retries=0,
    )


# ---------------------------------------------------------------------------
# Retry engine — works for every Stainless-style SDK (groq, cerebras, openai)
# ---------------------------------------------------------------------------
def _sdk_exception_tuples() -> Tuple[tuple, tuple, tuple]:
    """Collect (rate_limit, server_error, connection_error) exception classes
    from whichever LLM SDKs are installed. An empty tuple in an `except ()`
    clause is legal and simply never matches."""
    rate_limited, server_error, conn_error = [], [], []
    for mod_name in ("groq", "cerebras.cloud.sdk", "openai"):
        try:
            mod = importlib.import_module(mod_name)
        except ImportError:
            continue
        for target, name in (
            (rate_limited, "RateLimitError"),
            (server_error, "APIStatusError"),
            (conn_error, "APIConnectionError"),
        ):
            cls = getattr(mod, name, None)
            if isinstance(cls, type):
                target.append(cls)
    return tuple(rate_limited), tuple(server_error), tuple(conn_error)


def _retry_after_seconds(exc: Exception) -> Optional[float]:
    """Pull the provider's Retry-After hint out of a 429 response, if present."""
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None) or {}
    for key in ("retry-after", "Retry-After"):
        value = headers.get(key)
        if value:
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
    return None


def _with_retry(fn, label: str):
    """Run ``fn`` with retries on rate limits and transient server/conn errors."""
    RATE_LIMITED, SERVER_ERROR, CONN_ERROR = _sdk_exception_tuples()

    last_error: Optional[Exception] = None
    for attempt in range(1, GROQ_MAX_RETRIES + 1):
        try:
            return fn()
        except RATE_LIMITED as e:
            last_error = e
            # Retry-After is authoritative on free tiers; back off beyond it slightly.
            wait = (_retry_after_seconds(e) or min(10 * (2 ** (attempt - 1)), 120)) + random.uniform(0.5, 2.0)
            print(f"[llm] {label}: rate limited (attempt {attempt}/{GROQ_MAX_RETRIES}); "
                  f"sleeping {wait:.0f}s", flush=True)
            time.sleep(wait)
        except SERVER_ERROR as e:
            status = getattr(e, "status_code", 0) or 0
            # Retry transient 5xx; fail fast on other 4xx (bad key, bad request).
            if status and not (500 <= status < 600):
                raise
            last_error = e
            wait = min(10 * (2 ** (attempt - 1)), 120) + random.uniform(0.5, 2.0)
            print(f"[llm] {label}: server error {status} (attempt {attempt}/{GROQ_MAX_RETRIES}); "
                  f"sleeping {wait:.0f}s", flush=True)
            time.sleep(wait)
        except CONN_ERROR as e:
            last_error = e
            wait = min(5 * (2 ** (attempt - 1)), 60) + random.uniform(0.2, 1.0)
            print(f"[llm] {label}: connection error (attempt {attempt}/{GROQ_MAX_RETRIES}); "
                  f"sleeping {wait:.0f}s", flush=True)
            time.sleep(wait)

    raise RuntimeError(f"LLM call {label!r} failed after {GROQ_MAX_RETRIES} attempts: {last_error}")


# ---------------------------------------------------------------------------
# LLM backends + failover orchestrator
# ---------------------------------------------------------------------------
def _chat_call(client, model: str, prompt: str) -> str:
    response = client.chat.completions.create(
        model=model,
        temperature=0.4,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": _JSON_SYSTEM_MESSAGE},
            {"role": "user", "content": prompt},
        ],
    )
    return response.choices[0].message.content or ""


def call_groq_llm(prompt: str) -> str:
    """Groq LLM backend (PRIMARY)."""
    return _with_retry(
        lambda: _chat_call(get_client(), GROQ_LLM_MODEL, prompt),
        f"groq[{GROQ_LLM_MODEL}]",
    )


def call_cerebras_llm(prompt: str) -> str:
    """Cerebras LLM backend (FALLBACK — used when Groq hard-fails)."""
    return _with_retry(
        lambda: _chat_call(get_cerebras_client(), CEREBRAS_LLM_MODEL, prompt),
        f"cerebras[{CEREBRAS_LLM_MODEL}]",
    )


# Circuit breaker state: wall-clock time until which each cloud backend is
# short-circuited. A breaker only short-circuits when a LATER tier exists —
# with no fallback behind it a lone backend is always tried, exactly like the
# original single-backend path.
_groq_down_until: float = 0.0
_cerebras_down_until: float = 0.0


def _groq_healthy() -> bool:
    return time.time() >= _groq_down_until


def _trip_groq_breaker() -> None:
    global _groq_down_until
    _groq_down_until = time.time() + LLM_CIRCUIT_BREAK_SECONDS


def _cerebras_healthy() -> bool:
    return time.time() >= _cerebras_down_until


def _trip_cerebras_breaker() -> None:
    global _cerebras_down_until
    _cerebras_down_until = time.time() + LLM_CIRCUIT_BREAK_SECONDS


def call_llm(prompt: str) -> str:
    """THE one entry point for every prompt in this project.

    Chain: Groq → Cerebras → local llama.cpp CPU model. On a hard failure the
    IDENTICAL prompt replays on the next tier; tripped breakers route the rest
    of the run straight to the surviving tier(s) — nothing stops just because
    free tiers are down.
    """
    last_error: Optional[Exception] = None
    groq_has_fallback = cerebras_fallback_available() or local_llm_enabled()

    # ---- Tier 1: Groq ----
    if _groq_healthy() or not groq_has_fallback:
        try:
            return call_groq_llm(prompt)
        except Exception as e:
            if not groq_has_fallback:
                raise  # classic single-backend path — no change from old behavior
            last_error = e
            _trip_groq_breaker()
            print(f"[llm] ⚠ Groq LLM failed ({e}) — Groq skipped for "
                  f"{LLM_CIRCUIT_BREAK_SECONDS}s", flush=True)
    else:
        print("[llm] Groq circuit open — skipping to fallback tier", flush=True)

    # ---- Tier 2: Cerebras ----
    if cerebras_fallback_available():
        if last_error is not None:
            print("[llm] FAILING OVER to Cerebras with the identical prompt…", flush=True)
        if _cerebras_healthy() or not local_llm_enabled():
            try:
                return call_cerebras_llm(prompt)
            except Exception as e:
                if not local_llm_enabled():
                    raise
                last_error = e
                _trip_cerebras_breaker()
                print(f"[llm] ⚠ Cerebras LLM also failed ({e}) — Cerebras skipped for "
                      f"{LLM_CIRCUIT_BREAK_SECONDS}s", flush=True)
        else:
            print("[llm] Cerebras circuit open — skipping to local tier", flush=True)

    # ---- Tier 3: local llama.cpp — zero-API survival tier ----
    if local_llm_enabled():
        print("[llm] ⛰ DOOMSDAY TIER: running prompt on local CPU model "
              "(slower — needs no keys, no accounts, no API uptime)…", flush=True)
        try:
            return call_local_llm(prompt)
        except Exception as e:
            raise RuntimeError(
                f"All LLM tiers failed — clouds ({last_error}) — local tier ({e})"
            ) from e

    # local tier disabled: surface the real cloud error like the old path did
    assert last_error is not None
    raise last_error


# ---------------------------------------------------------------------------
# Audio transcription (Groq-only — Cerebras has no free Whisper equivalent)
# ---------------------------------------------------------------------------
def transcribe_audio_groq(audio_path: str, language: Optional[str] = None) -> dict:
    """Run one audio file through Groq's hosted Whisper; returns the verbose_json dict."""
    print(f"[groq] whisper {GROQ_WHISPER_MODEL} ← {audio_path}", flush=True)

    def _call():
        with open(audio_path, "rb") as fh:
            return get_client().audio.transcriptions.create(
                file=(audio_path.rsplit("/", 1)[-1], fh),
                model=GROQ_WHISPER_MODEL,
                response_format="verbose_json",
                timestamp_granularities=["segment"],
                temperature=0.0,
                language=language,
            )

    response = _with_retry(_call, f"transcriptions[{GROQ_WHISPER_MODEL}]")

    # groq>=0.9 returns pydantic models; be liberal about what we accept back.
    if hasattr(response, "model_dump"):
        return response.model_dump()
    if isinstance(response, dict):
        return response
    if isinstance(response, str):
        import json

        return json.loads(response)
    raise RuntimeError(f"Unexpected Groq transcription response type: {type(response)!r}")
