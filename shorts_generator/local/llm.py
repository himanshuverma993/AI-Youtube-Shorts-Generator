"""Local CPU LLM via llama.cpp — the DOOMSDAY tier-3 (zero keys needed).

When Groq AND Cerebras are both hard-down — or no keys are configured at all —
``call_llm`` replays the identical prompt here: a small quantized instruct
model running on this machine's own CPU. It is the survival tier: roughly
3–8 minutes per call on a 4-core GitHub runner instead of seconds on the
free clouds — but the pipeline stays 100% functional with zero money and
zero accounts.

Default model: Qwen2.5-3B-Instruct Q4_K_M (~1.9 GB, 32K-context chat model
that is unusually strong at JSON for its size). Override via
``LOCAL_LLM_REPO`` / ``LOCAL_LLM_FILE``. The first call downloads the weights
once into ``LOCAL_LLM_DIR``, which the campaign workflow caches between runs.

Why it is trustworthy at 3B: JSON validity is enforced at DECODE time by
llama.cpp's grammar engine (``response_format={"type": "json_object"}``) —
the model literally cannot emit broken JSON — and over-long prompts are
middle-truncated with the schema tail preserved, so a 3-hour video can't
overflow the context window and kill the run.
"""
import os
import re
import time
from functools import lru_cache
from typing import Optional

from ..config import (
    LOCAL_LLM_CTX,
    LOCAL_LLM_DIR,
    LOCAL_LLM_FILE,
    LOCAL_LLM_MAX_TOKENS,
    LOCAL_LLM_REPO,
    LOCAL_LLM_THREADS,
)

# Small-model guardrail: an explicit JSON contract as system message (hosted
# 70 B tiers already follow our inline schema text; 3 B needs it spelled out).
_LOCAL_SYSTEM_MESSAGE = (
    "You are a strict JSON API. The user describes a task and an exact output "
    "schema. Reply with ONE valid JSON object and nothing else — no markdown "
    "fences, no commentary, no apologies, no trailing text."
)


def _split_series_prefix(name: str):
    """'X-Q4_K_M-00001-of-00003.gguf' → ('X-Q4_K_M', True); else (name, False)."""
    m = re.match(r"^(.*?)-\d{5}-of-\d{5}\.gguf$", name, re.IGNORECASE)
    return (m.group(1), True) if m else (name[:-5], False)


def _resolve_model_path() -> str:
    """Local path of the GGUF weights, downloading once if needed.

    Self-healing: if LOCAL_LLM_FILE was renamed upstream, list the repo and
    pick the best single-file q4_k_m — or download the whole part-series when
    only split files exist — instead of dying on a 404.
    """
    try:
        from huggingface_hub import hf_hub_download, list_repo_files  # type: ignore
    except ImportError as e:
        raise RuntimeError(
            "huggingface-hub is required for the tier-3 local LLM. Install with:\n"
            "    pip install -r requirements.txt"
        ) from e

    os.makedirs(LOCAL_LLM_DIR, exist_ok=True)
    try:
        return hf_hub_download(LOCAL_LLM_REPO, LOCAL_LLM_FILE, local_dir=LOCAL_LLM_DIR)
    except Exception as exact_err:
        print(f"[llm/local] exact file '{LOCAL_LLM_FILE}' unavailable ({exact_err}) — "
              f"scanning repo for a q4_k_m alternative…", flush=True)
        ggufs = [f for f in list_repo_files(LOCAL_LLM_REPO) if f.lower().endswith(".gguf")]
        singles = [f for f in ggufs
                   if "q4_k_m" in f.lower() and not _split_series_prefix(f)[1]]
        if singles:
            chosen = sorted(singles)[0]
        else:
            first_parts = [f for f in ggufs
                           if "q4_k_m" in f.lower()
                           and re.search(r"-00001-of-\d+\.gguf$", f, re.IGNORECASE)]
            if not first_parts:
                raise RuntimeError(
                    f"No q4_k_m GGUF found in {LOCAL_LLM_REPO}. "
                    f"Files seen: {ggufs[:10]}. Set LOCAL_LLM_FILE manually."
                ) from exact_err
            prefix = _split_series_prefix(sorted(first_parts)[0])[0]
            siblings = sorted(f for f in ggufs if f.startswith(prefix))
            print(f"[llm/local] split model — downloading all {len(siblings)} "
                  f"part(s) of '{prefix}' (llama.cpp needs every part)", flush=True)
            for part in siblings:
                hf_hub_download(LOCAL_LLM_REPO, part, local_dir=LOCAL_LLM_DIR)
            chosen = sorted(first_parts)[0]
        print(f"[llm/local] resolved to '{chosen}'", flush=True)
        return hf_hub_download(LOCAL_LLM_REPO, chosen, local_dir=LOCAL_LLM_DIR)


@lru_cache(maxsize=1)
def _model():
    """Process-wide lazy Llama instance (first call loads ~2 GB)."""
    try:
        from llama_cpp import Llama  # type: ignore
    except ImportError as e:
        raise RuntimeError(
            "llama-cpp-python is required for the tier-3 local LLM. Install with:\n"
            "    pip install -r requirements.txt"
        ) from e
    path = _resolve_model_path()
    print(f"[llm/local] loading {path} (ctx={LOCAL_LLM_CTX}, "
          f"threads={LOCAL_LLM_THREADS or 'auto'})", flush=True)
    return Llama(
        model_path=path,
        n_ctx=LOCAL_LLM_CTX,
        n_threads=LOCAL_LLM_THREADS or None,
        n_batch=512,
        verbose=False,
    )


def _fit_prompt_to_ctx(prompt: str, max_tokens: int) -> str:
    """Keep head+tail, cut the MIDDLE if the prompt would overflow n_ctx.

    The transcript sits between the task rules (head) and the output-schema
    instructions (tail): middle-truncation loses some middle transcript lines
    but preserves the instructions the model still needs. Logged loudly —
    doomsday tier degrades visibly, never silently.
    """
    budget_chars = max((LOCAL_LLM_CTX - max_tokens - 128) * 4 - len(_LOCAL_SYSTEM_MESSAGE), 2000)
    if len(prompt) <= budget_chars:
        return prompt
    head = int(budget_chars * 0.35)
    tail = budget_chars - head
    print(f"[llm/local] ⚠ prompt {len(prompt)} chars > ctx budget {budget_chars} — "
          f"middle-truncating transcript (schema tail preserved)", flush=True)
    return prompt[:head] + "\n[…middle of transcript truncated to fit local context…]\n" + prompt[-tail:]


def call_local_llm(prompt: str, max_tokens: Optional[int] = None) -> str:
    """Run one prompt on the local instruct model; returns the raw reply text.
    Same contract as the hosted backends in groq_client."""
    model = _model()
    max_tokens = max_tokens or LOCAL_LLM_MAX_TOKENS
    prompt = _fit_prompt_to_ctx(prompt, max_tokens)
    t0 = time.time()
    completion = model.create_chat_completion(
        messages=[
            {"role": "system", "content": _LOCAL_SYSTEM_MESSAGE},
            {"role": "user", "content": prompt},
        ],
        temperature=0.4,
        max_tokens=max_tokens,
        # Grammar-constrained decoding: llama.cpp enforces token-level JSON
        # validity, so a 3B fallback can't emit unparseable output.
        response_format={"type": "json_object"},
    )
    text = (completion["choices"][0]["message"].get("content") or "").strip()
    print(f"[llm/local] reply: {len(text)} chars in {time.time() - t0:.0f}s (cpu)",
          flush=True)
    return text
