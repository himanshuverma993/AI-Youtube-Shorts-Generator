"""Central configuration.

This fork is wired for a $0-cost stack:
  * Groq free tier       — hosted Whisper transcription + primary LLM
  * Cerebras free tier   — automatic LLM fallback (no credit card)
  * faster-whisper local — automatic transcription fallback (pure CPU, no API)
  * yt-dlp / ffmpeg / OpenCV — download + clipping + vertical reframe (local)

No OpenAI, Gemini, or MuAPI keys are used anywhere in this codebase.
"""
import os

from dotenv import load_dotenv

load_dotenv()

# --------------------------------------------------------------------------
# Groq (free tier) — get a key at https://console.groq.com/keys
# One key powers both the Whisper transcription endpoint and the chat models.
# --------------------------------------------------------------------------
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
# Hosted Whisper for transcription. whisper-large-v3-turbo is the cheapest
# free-tier option with segment timestamps (verbose_json).
GROQ_WHISPER_MODEL = os.getenv("GROQ_WHISPER_MODEL", "whisper-large-v3-turbo").strip()
# Hosted Llama for highlight ranking + metadata generation.
GROQ_LLM_MODEL = os.getenv("GROQ_LLM_MODEL", "llama-3.3-70b-versatile").strip()
GROQ_TIMEOUT_SECONDS = float(os.getenv("GROQ_TIMEOUT", "300"))
# Retry budget shared by BOTH LLM backends (Groq primary + Cerebras fallback).
GROQ_MAX_RETRIES = int(os.getenv("GROQ_MAX_RETRIES", "5"))

# ---------------------------------------------------------------------------
# Cerebras (free tier) — automatic LLM fallback when Groq hard-fails.
# 1M free tokens/day, no credit card — https://cloud.cerebras.ai
# NOTE: Cerebras has no free Whisper, so audio TRANSCRIPTION stays on Groq;
# the failover only covers chat/LLM calls (highlights + metadata).
# ---------------------------------------------------------------------------
CEREBRAS_API_KEY = os.getenv("CEREBRAS_API_KEY", "").strip()
CEREBRAS_LLM_MODEL = os.getenv("CEREBRAS_LLM_MODEL", "llama-3.3-70b").strip()
LLM_FALLBACK_ENABLED = os.getenv("LLM_FALLBACK", "true").strip().lower() == "true"
# Circuit breaker: once Groq's LLM hard-fails, skip it for this long so every
# later LLM call in the run goes straight to Cerebras instead of paying the
# retry-sleep tax per call. After the cooldown, Groq is probed again.
LLM_CIRCUIT_BREAK_SECONDS = int(os.getenv("LLM_CIRCUIT_BREAK_SECONDS", "300"))

# Groq's audio transcription endpoint caps free-tier uploads at ~25 MiB per
# request. Audio is downmixed to 32 kbps mono mp3 (~14 MB/hour); anything
# that still exceeds the cap gets chunked before upload.
GROQ_MAX_AUDIO_BYTES = int(os.getenv("GROQ_MAX_AUDIO_BYTES", str(24 * 1024 * 1024)))
AUDIO_CHUNK_SECONDS = int(os.getenv("AUDIO_CHUNK_SECONDS", "600"))  # 10-min chunks

# --------------------------------------------------------------------------
# Local storage / rendering
# --------------------------------------------------------------------------
OUTPUT_DIR = os.getenv("OUTPUT_DIR", "output")
AUDIO_BITRATE = os.getenv("AUDIO_BITRATE", "32k")       # mono speech mp3 for transcription
AUDIO_SAMPLE_RATE = os.getenv("AUDIO_SAMPLE_RATE", "16000")

# ---------------------------------------------------------------------------
# Local Whisper fallback (runs on THIS machine's CPU — zero API dependency)
# If Groq Whisper is down or out of free-tier quota, transcription continues
# locally via faster-whisper. Default "small" int8 ≈ 7–8 min per hour of
# podcast audio on a 4-core GitHub runner; "tiny"/"base" trade quality for
# speed, "medium" the other way.
# ---------------------------------------------------------------------------
LOCAL_WHISPER_MODEL = os.getenv("LOCAL_WHISPER_MODEL", "small").strip()    # tiny/base/small/medium
LOCAL_WHISPER_DEVICE = os.getenv("LOCAL_WHISPER_DEVICE", "cpu").strip()    # cpu / cuda
LOCAL_WHISPER_COMPUTE = os.getenv("LOCAL_WHISPER_COMPUTE", "").strip()     # "" → int8 (float16 if cuda)
WHISPER_FALLBACK_ENABLED = os.getenv("WHISPER_FALLBACK", "true").strip().lower() == "true"
# Circuit breaker (same idea as the LLM side): after a Groq Whisper failure,
# skip Groq for this long and transcribe locally instead of paying retry-sleep.
WHISPER_CIRCUIT_BREAK_SECONDS = int(os.getenv("WHISPER_CIRCUIT_BREAK_SECONDS", "300"))

# ---------------------------------------------------------------------------
# Local LLM — the DOOMSDAY tier-3 (zero keys, zero accounts, zero internet-API)
# When Groq AND Cerebras are both dead/not configured, every prompt replays on
# a small instruct model running on this machine's CPU via llama.cpp.
# Default: Qwen2.5-3B-Instruct Q4_K_M (~1.9 GB, 32K ctx, strong JSON-for-size)
# ≈ 3–8 min per call on a 4-core runner. Built to survive, not to sprint.
# Weights land in LOCAL_LLM_DIR (the Actions workflow caches that directory).
# ---------------------------------------------------------------------------
LOCAL_LLM_ENABLED = os.getenv("LOCAL_LLM", "true").strip().lower() == "true"
LOCAL_LLM_REPO = os.getenv("LOCAL_LLM_REPO", "bartowski/Qwen2.5-3B-Instruct-GGUF").strip()
LOCAL_LLM_FILE = os.getenv("LOCAL_LLM_FILE", "Qwen2.5-3B-Instruct-Q4_K_M.gguf").strip()
LOCAL_LLM_DIR = os.getenv("LOCAL_LLM_DIR", "models").strip()
LOCAL_LLM_CTX = int(os.getenv("LOCAL_LLM_CTX", "16384"))
LOCAL_LLM_THREADS = int(os.getenv("LOCAL_LLM_THREADS", "0"))       # 0 = library auto
LOCAL_LLM_MAX_TOKENS = int(os.getenv("LOCAL_LLM_MAX_TOKENS", "2048"))

# ---------------------------------------------------------------------------
# Trend context (Phase-1 Fix 2) — OPTIONAL, zero-key sources, fail-soft.
# OFF by default until the operator names the campaign niche (generic trends
# without a niche are noise). When on, a "what is working right now" block
# (yt-dlp search + Google Trends RSS) is injected into highlight + metadata
# prompts as framing bias only — the transcript stays the source of truth.
# ---------------------------------------------------------------------------
TREND_CONTEXT_ENABLED = os.getenv("TREND_CONTEXT", "true").strip().lower() == "true"
CAMPAIGN_NICHE = os.getenv("CAMPAIGN_NICHE", "").strip()           # e.g. "ai startup podcast"
TRENDS_GEO = os.getenv("TRENDS_GEO", "IN").strip()                 # Google Trends geo
TREND_CACHE_HOURS = int(os.getenv("TREND_CACHE_HOURS", "24"))
TREND_MAX_ITEMS = int(os.getenv("TREND_MAX_ITEMS", "10"))

# ---------------------------------------------------------------------------
# Channel feedback loop (Phase-2 Fix 3A) — OPTIONAL, off until OAuth is set.
# YouTube Analytics API (channel-owner read-only OAuth; FREE — no billing).
# The runner exchanges YT_REFRESH_TOKEN (set it via scripts/oauth_local_setup.py
# ONCE, then store the 3 values as GitHub secrets). Feedback merges into the
# prompt only after FEEDBACK_MIN_POSTS measured posts exist.
# ---------------------------------------------------------------------------
FEEDBACK_ENABLED = os.getenv("FEEDBACK", "true").strip().lower() == "true"
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "").strip()
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "").strip()
YT_REFRESH_TOKEN = os.getenv("YT_REFRESH_TOKEN", "").strip()
FEEDBACK_MIN_POSTS = int(os.getenv("FEEDBACK_MIN_POSTS", "5"))
FEEDBACK_MIN_AGE_HOURS = int(os.getenv("FEEDBACK_MIN_AGE_HOURS", "48"))

# ---------------------------------------------------------------------------
# YouTube auto-upload (Phase-3 Fix 4A) — OFF by default (dry-run philosophy:
# artifacts flow never changes until the operator explicitly opts in). Uses
# the SAME OAuth trio as the feedback loop (minted with the youtube.upload
# scope — rerun scripts/oauth_local_setup.py once if your token predates it).
# Quota: videos.insert costs 1600 units of the FREE 10,000/day budget → a
# hard daily ceiling plus a per-run cap; everything else waits in the queue.
# ---------------------------------------------------------------------------
UPLOAD_ENABLED = os.getenv("UPLOAD_ENABLED", "false").strip().lower() == "true"
UPLOAD_MAX_PER_RUN = int(os.getenv("UPLOAD_MAX_PER_RUN", "3"))
# Safe ladder: "private" by default — flip to "public" deliberately when ready.
YT_PRIVACY = os.getenv("YT_PRIVACY", "private").strip()           # private/unlisted/public
YT_CATEGORY_ID = os.getenv("YT_CATEGORY_ID", "22").strip()        # 22 = People & Blogs
UPLOAD_DAILY_CAP = int(os.getenv("UPLOAD_DAILY_CAP", "6"))        # ~6 × 1600 = 9,600 units
UPLOAD_MAX_ATTEMPTS = int(os.getenv("UPLOAD_MAX_ATTEMPTS", "3"))  # queue 3-strike, same ethos

# ---------------------------------------------------------------------------
# Instagram auto-upload + IG insights (Phase-4 Fix 4B/3B) — OFF by default.
# Needs: IG Business/Creator account + linked FB Page + FB Developer app
# (free; your own account posts work in Development mode — no app review for
# self-posting). Seed one long-lived (60-day) user token as IG_ACCESS_TOKEN;
# the runner rolls it forward automatically via the fb_exchange_token grant
# (cached in campaign/ig_token.json) so it effectively never expires while
# the campaign runs weekly or better.
# Clip hosting: IG's container API requires a PUBLIC media URL, so clips are
# published to a 'media-staging' GitHub Release in this repo (requires the
# repo to be PUBLIC) using the built-in GITHUB_TOKEN, then deleted post-use.
# ---------------------------------------------------------------------------
IG_UPLOAD_ENABLED = os.getenv("IG_UPLOAD_ENABLED", "false").strip().lower() == "true"
IG_ACCESS_TOKEN = os.getenv("IG_ACCESS_TOKEN", "").strip()        # long-lived seed token
IG_USER_ID = os.getenv("IG_USER_ID", "").strip()                  # 1784… IG business-account id
IG_UPLOAD_MAX_PER_RUN = int(os.getenv("IG_UPLOAD_MAX_PER_RUN", "3"))
IG_MAX_ATTEMPTS = int(os.getenv("IG_MAX_ATTEMPTS", "3"))
IG_CONTAINER_POLL_SECONDS = int(os.getenv("IG_CONTAINER_POLL_SECONDS", "10"))
IG_CONTAINER_TIMEOUT_SECONDS = int(os.getenv("IG_CONTAINER_TIMEOUT_SECONDS", "600"))
IG_UPLOAD_STAGING_TAG = os.getenv("IG_UPLOAD_STAGING_TAG", "media-staging")
# Optional but recommended: the FB app's own id/secret — needed ONLY for the
# rolling 60-day token refresh (uploads/insights themselves use the user
# token alone). Without these, re-seed IG_ACCESS_TOKEN manually every ~60 days.
IG_APP_ID = os.getenv("IG_APP_ID", "").strip()
IG_APP_SECRET = os.getenv("IG_APP_SECRET", "").strip()
# Feedback side (Phase-4 3B): IG insights metrics (no retention% exists on IG —
# documented honestly; scoring adapts to reach-velocity + shares + saves).
IG_FEEDBACK_ENABLED = os.getenv("IG_FEEDBACK_ENABLED", "true").strip().lower() == "true"

# --------------------------------------------------------------------------
# FTC compliance — appended to every generated description, no exceptions.
# --------------------------------------------------------------------------
FTC_DISCLOSURE_TAGS = os.getenv("FTC_DISCLOSURE_TAGS", "#ad #sponsored").strip()


def require_groq_key() -> str:
    if not GROQ_API_KEY:
        raise RuntimeError(
            "GROQ_API_KEY is not set. Create a free key at https://console.groq.com/keys "
            "and add it to your .env file or export it as an environment variable."
        )
    return GROQ_API_KEY


def require_cerebras_key() -> str:
    if not CEREBRAS_API_KEY:
        raise RuntimeError(
            "CEREBRAS_API_KEY is not set. Create a free key at https://cloud.cerebras.ai "
            "(1M free tokens/day, no credit card) and add it to .env."
        )
    return CEREBRAS_API_KEY


def cerebras_fallback_available() -> bool:
    """True when the Cerebras LLM fallback is configured and enabled."""
    return LLM_FALLBACK_ENABLED and bool(CEREBRAS_API_KEY)


def local_llm_enabled() -> bool:
    """True when the tier-3 local llama.cpp survival backend is armed."""
    return LOCAL_LLM_ENABLED
