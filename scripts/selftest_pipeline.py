#!/usr/bin/env python3
"""Offline self-tests for the render-accounting and strike-classification bugs
found while auditing campaign run 36494468151.

    python3 scripts/selftest_pipeline.py     # exit 0 = all assertions pass

Covers
------
A. pipeline.generate_shorts must NOT report unrendered clips as shorts.
   crop_highlights_local never raises; it returns {"clip_url": None, "error":..}
   placeholders. Leaving those in "shorts" made a run where every render failed
   look successful: campaign_runner counted them, marked the URL processed for
   good, and shipped zero files.

B. campaign_runner.is_transient_failure must separate "the runner had a bad
   day" (bot-check, network, 5xx, rate limit) from "this URL is hopeless"
   (no speech, private video). Only the latter may spend a 3-strike attempt —
   otherwise a healthy video is retired because YouTube bot-checked a
   datacenter IP once.

C. clipper._sane_fps must reject NaN/negative/absurd frame rates. The old
   `cap.get(...) or 30.0` only caught 0.0; NaN is truthy and sailed through
   into VideoWriter, which then silently discarded every frame.

No network, no ffmpeg, no API keys required.
"""
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

FAILS = 0


def check(cond, label):
    global FAILS
    print(("   PASS  " if cond else "   FAIL  ") + label)
    if not cond:
        FAILS += 1


# --------------------------------------------------------------------------- #
print("=" * 78)
print("A. pipeline: unrendered clips must never be counted as shorts")
print("=" * 78)

import shorts_generator.pipeline as pipe

TRANSCRIPT = {"language": "hi", "duration": 600.0,
              "segments": [{"start": 0.0, "end": 5.0, "text": "नमस्ते"}]}
HIGHLIGHTS = [
    {"title": "one", "start_time": 10.0, "end_time": 35.0, "score": 90,
     "hook_sentence": "hook one", "virality_reason": "r"},
    {"title": "two", "start_time": 60.0, "end_time": 85.0, "score": 80,
     "hook_sentence": "hook two", "virality_reason": "r"},
    {"title": "three", "start_time": 120.0, "end_time": 145.0, "score": 70,
     "hook_sentence": "hook three", "virality_reason": "r"},
]


def run_pipeline(crop_result):
    """Drive generate_shorts with every external stage stubbed out."""
    pipe.download_youtube_local = lambda url, fmt=None, **k: "/tmp/source.mp4"
    pipe.transcribe = lambda path, language=None: TRANSCRIPT
    pipe.get_highlights = lambda t, num_clips=3, context_block=None: {
        "highlights": list(HIGHLIGHTS)}
    pipe.crop_highlights_local = lambda src, top, aspect_ratio="9:16", out_dir=None: crop_result
    pipe.generate_metadata = lambda shorts, transcript=None, context_block=None: [
        {**s, "metadata": {"youtube": {"title": "t", "description": "d", "hashtags": []},
                           "instagram": {"caption": "c", "hashtags": []}}} for s in shorts]
    pipe.write_metadata_sidecars = lambda shorts: None
    return pipe.generate_shorts("https://youtu.be/x", num_clips=3)


print("\nA1: ALL renders fail -> must RAISE (so the URL is retried, not retired)")
allfail = [{**h, "clip_url": None, "error": "ffmpeg exploded"} for h in HIGHLIGHTS]
try:
    res = run_pipeline(allfail)
    check(False, f"raised — instead returned {len(res['shorts'])} 'shorts'")
except RuntimeError as e:
    check("All 3 clip(s) failed to render" in str(e), f"RuntimeError raised: {str(e)[:70]}...")
    check("ffmpeg exploded" in str(e), "error message surfaces the underlying cause")
except Exception as e:
    check(False, f"wrong exception type {type(e).__name__}: {e}")

print("\nA2: PARTIAL failure -> only rendered clips in 'shorts', rest in 'failed_clips'")
partial = [
    {**HIGHLIGHTS[0], "clip_url": "/out/short_01.mp4"},
    {**HIGHLIGHTS[1], "clip_url": None, "error": "codec error"},
    {**HIGHLIGHTS[2], "clip_url": "/out/short_03.mp4"},
]
res = run_pipeline(partial)
check(len(res["shorts"]) == 2, f"shorts == 2 rendered (got {len(res['shorts'])})")
check(all(s.get("clip_url") for s in res["shorts"]), "every entry in shorts has a real clip_url")
check(len(res.get("failed_clips", [])) == 1, "the 1 failure is recorded in failed_clips")
check(res["failed_clips"][0].get("error") == "codec error", "failure keeps its error string")
check(all("metadata" in s for s in res["shorts"]), "metadata generated for rendered clips only")

print("\nA3: ALL succeed -> unchanged happy path")
ok = [{**h, "clip_url": f"/out/short_{i:02d}.mp4"} for i, h in enumerate(HIGHLIGHTS, 1)]
res = run_pipeline(ok)
check(len(res["shorts"]) == 3, "all 3 present")
check(res.get("failed_clips") == [], "failed_clips empty")

# --------------------------------------------------------------------------- #
print("\n" + "=" * 78)
print("B. campaign_runner: infra failures must not spend a strike")
print("=" * 78)

from campaign_runner import is_transient_failure

TRANSIENT = [
    "ERROR: [youtube] X: Sign in to confirm you\u2019re not a bot. Use --cookies-from-browser",
    "YouTube refused every InnerTube client (default, tv_simply) for https://...",
    "HTTPSConnectionPool(host='api.groq.com', port=443): Max retries exceeded",
    "Connection reset by peer",
    "urllib.error: 503 Service Unavailable",
    "429 Too Many Requests: rate limit reached for whisper-large-v3-turbo",
    "The read operation timed out",
    "500 Server Error: Internal Server Error for url",
]
CONTENT = [
    "Whisper produced no segments. The video may have no detectable speech.",
    "Highlight generator returned zero clips.",
    "ERROR: [youtube] xxxx: Private video. Sign in if you've been granted access",
    "ERROR: [youtube] xxxx: Video unavailable. This video has been removed by the uploader",
    "All 3 clip(s) failed to render — no shorts produced. First error: no frames could be decoded",
]
print("\nB1: transient/infra -> True (retry forever, no strike)")
for m in TRANSIENT:
    check(is_transient_failure(Exception(m)), f"transient: {m[:62]}")
print("\nB2: content/permanent -> False (spend a strike, retire after 3)")
for m in CONTENT:
    check(not is_transient_failure(Exception(m)), f"permanent: {m[:62]}")

# --------------------------------------------------------------------------- #
print("\n" + "=" * 78)
print("C. clipper._sane_fps: reject NaN / negative / absurd frame rates")
print("=" * 78)

from shorts_generator.local.clipper import _sane_fps

cases = [
    (25.0, 25.0, "normal 25fps preserved"),
    (59.94, 59.94, "fractional 59.94 preserved"),
    (0.0, 30.0, "0.0 -> default"),
    (float("nan"), 30.0, "NaN -> default (the old `or 30.0` let this through)"),
    (float("inf"), 30.0, "inf -> default"),
    (-5.0, 30.0, "negative -> default"),
    (1e6, 30.0, "absurd 1000000 -> default"),
    (None, 30.0, "None -> default"),
    ("junk", 30.0, "non-numeric -> default"),
]
for raw, want, label in cases:
    check(_sane_fps(raw) == want, f"{label} (got {_sane_fps(raw)})")

print("\n" + "=" * 78)
print(f"RESULT: {'ALL CHECKS PASSED' if FAILS == 0 else str(FAILS) + ' CHECK(S) FAILED'}")
sys.exit(1 if FAILS else 0)
